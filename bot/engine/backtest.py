"""Historical walk-forward backtest with EV, MAE/MFE tracking."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from bot.domain.schemas import Signal, SymbolFrames, UniverseSymbol
from bot.engine import SignalEngine, StrategyRegistry
from bot.features.prepare import prepare_symbol
from bot.features.prepare_frame import min_required_bars
from bot.market.data import _timeframe_to_seconds
from bot.market.proxy_bootstrap import ensure_network_ready
from bot.market.rest_impl import BinanceClientImpl
from bot.runtime_policy import effective_engine_score_floor
from bot.setups.base import SetupParams
from bot.strategies import STRATEGY_CLASSES

if TYPE_CHECKING:
    from bot.domain.config import BotSettings

LOG = logging.getLogger("bot.engine.backtest")

_BACKTEST_MINIMUMS = min_required_bars(
    min_bars_15m=500,
    min_bars_1h=300,
    min_bars_5m=200,
    min_bars_4h=300,
)
_WARMUP_DAYS = 14
_MIN_BACKTEST_DAYS = 7


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    setup_id: str
    direction: str
    score: float
    entry_time: str
    exit_time: str
    result: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    mae_pct: float
    mfe_pct: float
    window_id: int = 0


def _build_engine(settings: BotSettings) -> SignalEngine:
    registry = StrategyRegistry()
    for strategy_cls in STRATEGY_CLASSES:
        setup_id = strategy_cls.setup_id
        enabled = bool(getattr(settings.setups, setup_id, False))
        strategy = strategy_cls(SetupParams(enabled=enabled), settings)
        registry.register(strategy, enabled=enabled)
    return SignalEngine(registry, settings)


def _universe_symbol(symbol: str, *, last_price: float) -> UniverseSymbol:
    base = symbol.removesuffix("USDT") or symbol
    return UniverseSymbol(
        symbol=symbol,
        base_asset=base,
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1_000_000_000.0,
        price_change_pct=0.0,
        last_price=last_price,
        strategy_fits=tuple(s.setup_id for s in STRATEGY_CLASSES),
    )


def _slice_frame(frame: pl.DataFrame, *, end_close: datetime) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    return frame.filter(pl.col("close_time") <= end_close)


def _build_frames_at(
    *,
    symbol: str,
    close_time: datetime,
    packs: dict[str, pl.DataFrame],
    last_close: float,
) -> SymbolFrames:
    bid = last_close * 0.9999
    ask = last_close * 1.0001
    return SymbolFrames(
        symbol=symbol,
        df_1h=_slice_frame(packs["1h"], end_close=close_time),
        df_15m=_slice_frame(packs["15m"], end_close=close_time),
        df_5m=_slice_frame(packs["5m"], end_close=close_time),
        df_4h=_slice_frame(packs["4h"], end_close=close_time),
        bid_price=bid,
        ask_price=ask,
        frame_source_flags=("backtest_rest",),
    )


@dataclass(frozen=True, slots=True)
class ExitSimResult:
    result: str
    pnl_pct: float
    exit_time: datetime | None
    mae_pct: float
    mfe_pct: float


def simulate_signal_exit(
    signal: Signal,
    forward_bars: pl.DataFrame,
    *,
    move_stop_to_break_even_on_tp1: bool = True,
) -> ExitSimResult:
    """Simulate TP/SL/expiry on OHLC bars, tracking MAE/MFE."""
    direction = str(signal.direction or "").strip().lower()
    entry = (float(signal.entry_low) + float(signal.entry_high)) / 2.0
    stop = float(signal.stop)
    tp1 = float(signal.take_profit_1)
    tp2 = float(signal.take_profit_2)
    valid_until = signal.valid_until
    if valid_until is not None and valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=UTC)

    mae: float = 0.0
    mfe: float = 0.0

    for row in forward_bars.iter_rows(named=True):
        bar_time = row.get("close_time")
        if not isinstance(bar_time, datetime):
            continue
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=UTC)

        high = float(row.get("high") or 0.0)
        low = float(row.get("low") or 0.0)

        if direction == "long":
            adverse = (entry - low) / entry * 100.0
            favorable = (high - entry) / entry * 100.0
        else:
            adverse = (high - entry) / entry * 100.0
            favorable = (entry - low) / entry * 100.0
        mae = max(mae, adverse)
        mfe = max(mfe, favorable)

        if valid_until is not None and bar_time > valid_until:
            return ExitSimResult("expired", entry, bar_time, mae, mfe)

        if direction == "long":
            if low <= stop:
                pnl = (stop - entry) / entry * 100.0
                return ExitSimResult("stop_loss", pnl, bar_time, mae, mfe)
            if signal.single_target_mode and high >= tp1:
                pnl = (tp1 - entry) / entry * 100.0
                return ExitSimResult("tp1_hit", pnl, bar_time, mae, mfe)
            if high >= tp2:
                pnl = (tp2 - entry) / entry * 100.0
                return ExitSimResult("tp2_hit", pnl, bar_time, mae, mfe)
            if high >= tp1:
                if move_stop_to_break_even_on_tp1:
                    stop = entry
                pnl = (tp1 - entry) / entry * 100.0
                return ExitSimResult("tp1_hit", pnl, bar_time, mae, mfe)
        elif direction == "short":
            if high >= stop:
                pnl = (entry - stop) / entry * 100.0
                return ExitSimResult("stop_loss", pnl, bar_time, mae, mfe)
            if signal.single_target_mode and low <= tp1:
                pnl = (entry - tp1) / entry * 100.0
                return ExitSimResult("tp1_hit", pnl, bar_time, mae, mfe)
            if low <= tp2:
                pnl = (entry - tp2) / entry * 100.0
                return ExitSimResult("tp2_hit", pnl, bar_time, mae, mfe)
            if low <= tp1:
                if move_stop_to_break_even_on_tp1:
                    stop = entry
                pnl = (entry - tp1) / entry * 100.0
                return ExitSimResult("tp1_hit", pnl, bar_time, mae, mfe)

    last_time = forward_bars["close_time"][-1] if forward_bars.height else None
    if isinstance(last_time, datetime) and last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=UTC)
    return ExitSimResult("open_at_window_end", 0.0, last_time, mae, mfe)


async def _fetch_interval_history(
    client: BinanceClientImpl,
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    cursor = int(start_ms)
    while cursor < end_ms:
        chunk = await client.fetch_klines_between(
            symbol,
            interval,
            start_time_ms=cursor,
            end_time_ms=end_ms,
            limit=1500,
        )
        if chunk.is_empty():
            break
        frames.append(chunk)
        last_close = chunk["close_time"][-1]
        if hasattr(last_close, "timestamp"):
            cursor = int(last_close.timestamp() * 1000) + 1
        else:
            break
        if chunk.height < 1500:
            break
    if not frames:
        return pl.DataFrame()
    merged = pl.concat(frames, how="diagonal").unique(subset=["time"], keep="last")
    return merged.sort("close_time")


async def run_historical_backtest(
    settings: BotSettings,
    *,
    symbol: str,
    days: int,
    setup_id: str = "",
    interval: str = "15m",
    config_path: str = "config.toml",
    walk_forward_windows: int = 1,
) -> dict[str, Any]:
    """Walk-forward historical validation: detect → simulate TP/SL (no delivery).

    When ``walk_forward_windows > 1``, the evaluation period is split into N
    sequential windows and results are reported per-window plus aggregate,
    reducing overfit risk from single-period evaluation.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        msg = "backtest requires --symbol"
        raise ValueError(msg)

    walk_days = max(_MIN_BACKTEST_DAYS, int(days))
    event_interval = str(interval or "15m").strip().lower()
    if _timeframe_to_seconds(event_interval) is None:
        msg = f"unsupported backtest interval: {event_interval}"
        raise ValueError(msg)

    settings = await ensure_network_ready(settings, config_path=Path(config_path))
    client = BinanceClientImpl(
        rest_timeout_seconds=settings.ws.rest_timeout_seconds,
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
        network=settings.network,
    )
    try:
        return await _run_historical_backtest_impl(
            settings,
            client=client,
            normalized_symbol=normalized_symbol,
            walk_days=walk_days,
            event_interval=event_interval,
            setup_id=setup_id,
            walk_forward_windows=walk_forward_windows,
        )
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            await client.close()


def _build_time_windows(
    df: pl.DataFrame, n_windows: int
) -> list[tuple[datetime, datetime]]:
    """Split the time range of *df* into *n_windows* sequential segments."""
    if df.is_empty():
        return []
    times = df["close_time"].sort()
    start = times[0]
    end = times[-1]
    if n_windows <= 1:
        return [(start, end)]
    total = (end - start).total_seconds()
    chunk = total / n_windows
    windows: list[tuple[datetime, datetime]] = []
    for i in range(n_windows):
        ws = start + timedelta(seconds=i * chunk)
        we = start + timedelta(seconds=(i + 1) * chunk) if i < n_windows - 1 else end
        windows.append((ws, we))
    return windows


async def _run_historical_backtest_impl(
    settings: BotSettings,
    *,
    client: BinanceClientImpl,
    normalized_symbol: str,
    walk_days: int,
    event_interval: str,
    setup_id: str,
    walk_forward_windows: int = 1,
) -> dict[str, Any]:
    engine = _build_engine(settings)
    score_floor = effective_engine_score_floor(settings)

    end = datetime.now(UTC)
    window_start = end - timedelta(days=walk_days)
    fetch_start = window_start - timedelta(days=_WARMUP_DAYS)
    start_ms = int(fetch_start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    packs: dict[str, pl.DataFrame] = {}
    for tf in ("5m", "15m", "1h", "4h"):
        packs[tf] = await _fetch_interval_history(
            client,
            symbol=normalized_symbol,
            interval=tf,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    driver = packs.get(event_interval, pl.DataFrame())
    if driver.is_empty():
        return {
            "symbol": normalized_symbol,
            "days": walk_days,
            "interval": event_interval,
            "error": "no_kline_data",
            "trades": [],
            "summary": {},
        }

    setup_filter = frozenset({setup_id}) if setup_id else None
    trades: list[BacktestTrade] = []
    open_keys: set[tuple[str, str]] = set()
    bars_evaluated = 0
    signals_detected = 0

    driver_closed = driver.filter(pl.col("close_time") >= window_start)
    time_windows = _build_time_windows(driver_closed, n_windows=walk_forward_windows)
    move_be = bool(settings.tracking.move_stop_to_break_even_on_tp1)

    def _window_for(ct: datetime) -> int:
        for wid, (ws, we) in enumerate(time_windows):
            if ws <= ct <= we:
                return wid
        return 0

    for row in driver_closed.iter_rows(named=True):
        close_time = row.get("close_time")
        if not isinstance(close_time, datetime):
            continue
        if close_time.tzinfo is None:
            close_time = close_time.replace(tzinfo=UTC)
        last_close = float(row.get("close") or 0.0)
        frames = _build_frames_at(
            symbol=normalized_symbol,
            close_time=close_time,
            packs=packs,
            last_close=last_close,
        )
        prepared = prepare_symbol(
            _universe_symbol(normalized_symbol, last_price=last_close),
            frames,
            minimums=_BACKTEST_MINIMUMS,
            settings=settings,
        )
        if prepared is None:
            continue

        bars_evaluated += 1
        results = await engine.calculate_all(
            prepared,
            event_interval=event_interval,
            setup_subset=setup_filter,
        )
        forward = driver.filter(pl.col("close_time") > close_time).head(200)

        for result in results:
            signal = result.signal
            if signal is None:
                continue
            if float(signal.score) < score_floor:
                continue
            key = (signal.setup_id, signal.direction)
            if key in open_keys:
                continue
            open_keys.add(key)
            signals_detected += 1

            sim = simulate_signal_exit(
                signal,
                forward,
                move_stop_to_break_even_on_tp1=move_be,
            )
            entry_mid = (float(signal.entry_low) + float(signal.entry_high)) / 2.0
            if signal.direction == "long":
                exit_price = entry_mid * (1.0 + sim.pnl_pct / 100.0)
            else:
                exit_price = entry_mid * (1.0 - sim.pnl_pct / 100.0)
            trades.append(
                BacktestTrade(
                    symbol=normalized_symbol,
                    setup_id=signal.setup_id,
                    direction=signal.direction,
                    score=float(signal.score),
                    entry_time=close_time.isoformat(),
                    exit_time=sim.exit_time.isoformat() if sim.exit_time else "",
                    result=sim.result,
                    entry_price=entry_mid,
                    exit_price=exit_price,
                    pnl_pct=round(sim.pnl_pct, 4),
                    mae_pct=round(sim.mae_pct, 4),
                    mfe_pct=round(sim.mfe_pct, 4),
                    window_id=_window_for(close_time),
                )
            )
            if sim.result != "open_at_window_end":
                open_keys.discard(key)

    by_setup: dict[str, dict[str, Any]] = {}
    for trade in trades:
        bucket = by_setup.setdefault(
            trade.setup_id,
            {
                "total": 0, "wins": 0, "losses": 0,
                "avg_pnl_pct": 0.0, "results": {},
                "avg_win_pnl": 0.0, "avg_loss_pnl": 0.0,
                "avg_mae_pct": 0.0, "avg_mfe_pct": 0.0,
                "ev": 0.0,
            },
        )
        bucket["total"] += 1
        bucket["avg_pnl_pct"] += trade.pnl_pct
        bucket["avg_mae_pct"] += trade.mae_pct
        bucket["avg_mfe_pct"] += trade.mfe_pct
        bucket["results"][trade.result] = bucket["results"].get(trade.result, 0) + 1
        if trade.result in {"tp1_hit", "tp2_hit"}:
            bucket["wins"] += 1
            bucket["avg_win_pnl"] += trade.pnl_pct
        elif trade.result == "stop_loss":
            bucket["losses"] += 1
            bucket["avg_loss_pnl"] += trade.pnl_pct

    for bucket in by_setup.values():
        total = int(bucket["total"])
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        bucket["avg_pnl_pct"] = round(bucket["avg_pnl_pct"] / total, 4) if total else 0.0
        bucket["avg_mae_pct"] = round(bucket["avg_mae_pct"] / total, 4) if total else 0.0
        bucket["avg_mfe_pct"] = round(bucket["avg_mfe_pct"] / total, 4) if total else 0.0
        bucket["win_rate"] = round(wins / total, 4) if total else 0.0
        avg_win = round(bucket["avg_win_pnl"] / wins, 4) if wins else 0.0
        avg_loss = round(bucket["avg_loss_pnl"] / losses, 4) if losses else 0.0
        bucket["avg_win_pnl"] = avg_win
        bucket["avg_loss_pnl"] = avg_loss
        if wins + losses > 0:
            wr = wins / (wins + losses)
            bucket["ev"] = round(
                wr * avg_win - (1.0 - wr) * abs(avg_loss), 4
            ) if avg_loss != 0 else 0.0

    executed = [t for t in trades if t.result not in {"open_at_window_end", "expired"}]
    wins = sum(1 for t in executed if t.result in {"tp1_hit", "tp2_hit"})
    losses = sum(1 for t in executed if t.result == "stop_loss")

    summary: dict[str, Any] = {
        "total_trades": len(trades),
        "executed": len(executed),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(executed), 4) if executed else 0.0,
        "avg_pnl_pct": round(
            sum(t.pnl_pct for t in executed) / len(executed), 4,
        ) if executed else 0.0,
        "avg_mae_pct": round(
            sum(t.mae_pct for t in executed) / len(executed), 4,
        ) if executed else 0.0,
        "avg_mfe_pct": round(
            sum(t.mfe_pct for t in executed) / len(executed), 4,
        ) if executed else 0.0,
        "by_setup": by_setup,
    }

    executed_wins = [t for t in executed if t.result in {"tp1_hit", "tp2_hit"}]
    executed_losses = [t for t in executed if t.result == "stop_loss"]
    if executed_wins and executed_losses:
        avg_win = sum(t.pnl_pct for t in executed_wins) / len(executed_wins)
        avg_loss = sum(t.pnl_pct for t in executed_losses) / len(executed_losses)
        summary["avg_win_pnl"] = round(avg_win, 4)
        summary["avg_loss_pnl"] = round(avg_loss, 4)
        wr = wins / (wins + losses)
        summary["ev"] = round(wr * avg_win - (1.0 - wr) * abs(avg_loss), 4)

    tp1_hits = sum(1 for t in executed if t.result == "tp1_hit")
    tp2_hits = sum(1 for t in executed if t.result == "tp2_hit")
    summary["tp1_hits"] = tp1_hits
    summary["tp2_hits"] = tp2_hits
    if wins > 0:
        summary["tp1_hit_rate"] = round(tp1_hits / wins, 4)
        summary["tp2_hit_rate"] = round(tp2_hits / wins, 4)

    tp_distances: list[float] = []
    for trade in trades:
        signal = trade.signal if hasattr(trade, "signal") else None
        if signal is None:
            continue
        entry = (float(signal.entry_low) + float(signal.entry_high)) / 2.0
        tp1 = getattr(signal, "take_profit_1", None)
        tp2 = getattr(signal, "take_profit_2", None)
        if entry > 0.0:
            if tp1 is not None and float(tp1) > 0.0:
                tp_distances.append(abs(float(tp1) - entry) / entry * 100.0)
            if tp2 is not None and float(tp2) > 0.0:
                tp_distances.append(abs(float(tp2) - entry) / entry * 100.0)
    if tp_distances:
        summary["avg_tp_distance_pct"] = round(sum(tp_distances) / len(tp_distances), 4)

    if walk_forward_windows > 1 and time_windows:
        by_window: dict[int, dict[str, Any]] = {}
        for trade in executed:
            w = by_window.setdefault(
                trade.window_id,
                {"window_id": trade.window_id, "trades": 0, "wins": 0, "losses": 0},
            )
            w["trades"] += 1
            if trade.result in {"tp1_hit", "tp2_hit"}:
                w["wins"] += 1
            elif trade.result == "stop_loss":
                w["losses"] += 1
        for w in by_window.values():
            total = w["trades"]
            w["win_rate"] = round(w["wins"] / total, 4) if total else 0.0
        summary["walk_forward"] = sorted(by_window.values(), key=lambda x: x["window_id"])

    return {
        "symbol": normalized_symbol,
        "days": walk_days,
        "interval": event_interval,
        "setup_filter": setup_id or None,
        "score_floor": score_floor,
        "bars_evaluated": bars_evaluated,
        "signals_detected": signals_detected,
        "trades": [trade.__dict__ for trade in trades],
        "summary": summary,
    }
