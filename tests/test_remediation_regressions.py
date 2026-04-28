from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import polars as pl
import pytest

from bot.application.bot import SignalBot
from bot.application import symbol_analyzer as symbol_analyzer_module
from bot.application.shortlist_service import (
    FALLBACK_REASON_FULL_REFRESH_DUE,
    FALLBACK_REASON_REFRESH_EXCEPTION,
    FALLBACK_REASON_USING_CACHED,
    FALLBACK_REASON_USING_PINNED,
    FALLBACK_REASON_WS_CACHE_COLD,
    SHORTLIST_FALLBACK_REASONS,
    ShortlistService,
)
from bot.application.symbol_analyzer import SymbolAnalyzer
from bot.cli import _is_preformatted_log_stderr
from bot.confluence import ConfluenceEngine  # noqa: F401
from bot.config import BotSettings, load_settings
from bot.config import WSConfig  # noqa: F401
from bot.core.engine import SignalEngine, StrategyRegistry
from bot.core.events import BookTickerEvent  # noqa: F401
from bot.features import _ichimoku_lines, _swing_points, _weighted_moving_average  # noqa: F401
from bot.market_data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.ml import MLFilter
from bot.models import AggTrade, PipelineResult, PreparedSymbol, Signal, UniverseSymbol  # noqa: F401
from bot.scoring import _crowd_position
from bot.setup_base import BaseSetup, SetupParams
from bot.strategies.cvd_divergence import CVDDivergenceSetup
from bot.strategies.ema_bounce import EmaBounceSetup
from bot.strategies.fvg import FVGSetup
from bot.strategies.funding_reversal import FundingReversalSetup
from bot.strategies.hidden_divergence import HiddenDivergenceSetup
from bot.strategies.squeeze_setup import SqueezeSetup
from bot.strategies.structure_pullback import StructurePullbackSetup
from bot.strategies.wick_trap_reversal import WickTrapReversalSetup
from bot.setups import _build_signal
from bot.setups.utils import build_structural_targets
from bot.tracked_signals import TrackedSignalState
from bot.tracking import SignalTracker
from bot.websocket import subscriptions as ws_subscriptions
from bot.ws_manager import FuturesWSManager  # noqa: F401

UTC = timezone.utc


class TelemetryStub:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []
        self.symbol_rows: list[tuple[str, str, dict]] = []

    def append_jsonl(self, filename: str, row: dict) -> None:
        self.rows.append((filename, row))

    def append_symbol_jsonl(
        self, bucket: str, symbol: str, relative_name: str, row: dict
    ) -> None:
        self.symbol_rows.append((symbol, relative_name, row))


class DummyMemoryRepo:
    def __init__(self) -> None:
        self.active_rows: list[dict] = []
        self.saved_rows: list[dict] = []
        self.saved_outcomes: list[dict] = []
        self.setup_outcomes: list[tuple[str, str]] = []
        self.tracking_stats: dict[str, int] = {
            "signals_sent": 0,
            "activated": 0,
            "tp1_hit": 0,
            "tp2_hit": 0,
            "stop_loss": 0,
            "expired": 0,
            "ambiguous_exit": 0,
            "active": 0,
        }

    async def get_active_signals(
        self,
        symbol: str | None = None,
        status: str | None = None,
        include_closed: bool = False,
    ) -> list[dict]:
        rows = list(self.active_rows)
        if symbol is not None:
            rows = [row for row in rows if row.get("symbol") == symbol]
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        if not include_closed and status is None:
            rows = [row for row in rows if row.get("status") in {"pending", "active"}]
        return [dict(row) for row in rows]

    async def save_active_signal(self, signal_data: dict) -> None:
        payload = dict(signal_data)
        self.saved_rows.append(payload)
        tracking_id = str(payload["tracking_id"])
        self.active_rows = [
            row for row in self.active_rows if row.get("tracking_id") != tracking_id
        ]
        self.active_rows.append(payload)

    async def increment_tracking_stats(self, **deltas: int) -> None:
        for key, value in deltas.items():
            self.tracking_stats[key] = self.tracking_stats.get(key, 0) + int(value)

    async def get_tracking_stats(self) -> dict[str, int]:
        return dict(self.tracking_stats)

    async def record_setup_outcome(self, setup_id: str, outcome: str) -> float:
        self.setup_outcomes.append((setup_id, outcome))
        return 0.0

    async def save_signal_outcomes_batch(self, outcomes_data: list[dict]) -> None:
        self.saved_outcomes.extend(outcomes_data)


def make_universe_symbol(
    symbol: str = "BTCUSDT", price: float = 100.0
) -> UniverseSymbol:
    return UniverseSymbol(
        symbol=symbol,
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1_000_000.0,
        price_change_pct=1.0,
        last_price=price,
        shortlist_bucket="test",
    )


def make_indicator_frame(price: float = 100.0) -> pl.DataFrame:
    now = datetime.now(UTC)
    return pl.DataFrame(
        {
            "time": [now],
            "close_time": [now],
            "open": [price - 1.0],
            "high": [price + 1.0],
            "low": [price - 2.0],
            "close": [price],
            "volume": [1_000.0],
            "atr14": [2.0],
            "atr_pct": [2.0],
            "rsi14": [55.0],
            "adx14": [25.0],
            "volume_ratio20": [1.4],
            "macd_hist": [0.25],
            "ema20": [price - 1.0],
            "ema50": [price - 2.0],
            "ema200": [price - 5.0],
            "supertrend_dir": [1.0],
            "obv_above_ema": [1.0],
            "bb_pct_b": [0.62],
            "bb_width": [4.2],
        }
    )


def make_feature_frame(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volume_ratios: list[float] | None = None,
    rsi_values: list[float] | None = None,
    delta_ratios: list[float] | None = None,
    bb_widths: list[float] | None = None,
    bb_pct_bs: list[float] | None = None,
    kc_uppers: list[float] | None = None,
    kc_lowers: list[float] | None = None,
    atr: float = 2.0,
) -> pl.DataFrame:
    count = len(closes)
    assert count > 0
    opens = opens or list(closes)
    highs = highs or [max(o, c) + 1.0 for o, c in zip(opens, closes, strict=False)]
    lows = lows or [min(o, c) - 1.0 for o, c in zip(opens, closes, strict=False)]
    volume_ratios = volume_ratios or [1.4] * count
    rsi_values = rsi_values or [55.0] * count
    delta_ratios = delta_ratios or [0.5] * count
    bb_widths = bb_widths or [0.018] * count
    bb_pct_bs = bb_pct_bs or [0.5] * count
    kc_uppers = kc_uppers or [c + 1.0 for c in closes]
    kc_lowers = kc_lowers or [c - 1.0 for c in closes]
    start = datetime(2026, 4, 23, 0, 0, tzinfo=UTC)
    times = [start + timedelta(minutes=15 * idx) for idx in range(count)]
    return pl.DataFrame(
        {
            "time": times,
            "close_time": times,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1_000.0] * count,
            "atr14": [atr] * count,
            "atr_pct": [atr] * count,
            "rsi14": rsi_values,
            "adx14": [25.0] * count,
            "volume_ratio20": volume_ratios,
            "macd_hist": [0.25] * count,
            "ema20": [c - 1.0 for c in closes],
            "ema50": [c - 2.0 for c in closes],
            "ema200": [c - 5.0 for c in closes],
            "supertrend_dir": [1.0] * count,
            "obv_above_ema": [1.0] * count,
            "bb_pct_b": bb_pct_bs,
            "bb_width": bb_widths,
            "kc_upper": kc_uppers,
            "kc_lower": kc_lowers,
            "delta_ratio": delta_ratios,
        }
    )


def make_prepared(symbol: str = "BTCUSDT", price: float = 100.0) -> PreparedSymbol:
    return PreparedSymbol(
        universe=make_universe_symbol(symbol=symbol, price=price),
        work_1h=make_indicator_frame(price),
        work_15m=make_indicator_frame(price),
        work_4h=make_indicator_frame(price + 5.0),
        bid_price=price - 0.1,
        ask_price=price + 0.1,
        spread_bps=2.0,
        funding_rate=0.0008,
        oi_current=1_250_000.0,
        oi_change_pct=3.2,
        ls_ratio=1.18,
        liquidation_score=0.35,
        market_regime="trending",
    )


def make_runtime_settings(
    *,
    strict_data_quality: bool = True,
    throttle_seconds: float = 0.0,
    min_move_bps: float = 0.0,
):
    return SimpleNamespace(
        runtime=SimpleNamespace(
            strict_data_quality=strict_data_quality,
            diagnostic_trace_limit_per_symbol=20,
            max_signals_per_cycle=3,
        ),
        ws=SimpleNamespace(
            rest_timeout_seconds=0.1,
            intra_candle_throttle_seconds=throttle_seconds,
            intra_candle_min_move_bps=min_move_bps,
        ),
        intelligence=SimpleNamespace(
            max_consecutive_stop_losses=3,
            stop_loss_pause_hours=0,
            runtime_mode="signal_only",
        ),
        filters=SimpleNamespace(cooldown_minutes=30),
    )


def make_signal(symbol: str = "BTCUSDT", created_at: datetime | None = None) -> Signal:
    return Signal(
        symbol=symbol,
        setup_id="ema_bounce",
        direction="long",
        score=0.82,
        timeframe="15m",
        entry_low=99.5,
        entry_high=100.5,
        stop=97.5,
        take_profit_1=103.0,
        take_profit_2=105.0,
        created_at=created_at or datetime.now(UTC),
    )


def make_tracked_state(
    *,
    tracking_id: str = "BTCUSDT|ema_bounce|long|20260423T000000000000Z",
    direction: str = "long",
    status: str = "pending",
    created_at: datetime | None = None,
    pending_expires_at: datetime | None = None,
    active_expires_at: datetime | None = None,
    activated_at: datetime | None = None,
    stop: float = 97.5,
    tp1: float = 103.0,
    tp2: float = 105.0,
    single_target_mode: bool = False,
    target_integrity_status: str = "valid",
) -> TrackedSignalState:
    created = created_at or datetime.now(UTC)
    return TrackedSignalState(
        tracking_id=tracking_id,
        tracking_ref="ABC12345",
        signal_key="BTCUSDT|ema_bounce|long",
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction=direction,
        timeframe="15m",
        created_at=created.isoformat(),
        pending_expires_at=(
            pending_expires_at or (created + timedelta(minutes=10))
        ).isoformat(),
        active_expires_at=(
            active_expires_at or (created + timedelta(minutes=60))
        ).isoformat(),
        entry_low=99.5,
        entry_high=100.5,
        entry_mid=100.0,
        initial_stop=stop,
        stop=stop,
        stop_price=stop,
        take_profit_1=tp1,
        tp1_price=tp1,
        take_profit_2=tp2,
        tp2_price=tp2,
        single_target_mode=single_target_mode,
        target_integrity_status=target_integrity_status,
        score=0.82,
        risk_reward=2.0,
        reasons=("test",),
        signal_message_id=321,
        bias_4h="uptrend",
        quote_volume=1_000_000.0,
        spread_bps=2.0,
        atr_pct=2.0,
        orderflow_delta_ratio=0.2,
        status=status,
        activated_at=activated_at.isoformat() if activated_at is not None else None,
    )


def make_tracker(
    market_data: object,
) -> tuple[SignalTracker, DummyMemoryRepo, TelemetryStub]:
    repo = DummyMemoryRepo()
    telemetry = TelemetryStub()
    settings = SimpleNamespace(
        tracking=SimpleNamespace(
            enabled=True,
            pending_expiry_minutes=10,
            active_expiry_minutes=60,
            agg_trade_page_limit=4,
            agg_trade_page_size=100,
            move_stop_to_break_even_on_tp1=False,
        ),
        ws=SimpleNamespace(rest_timeout_seconds=0.1),
        features_store_file=None,
    )
    tracker = SignalTracker(
        settings,
        market_data=market_data,
        telemetry=telemetry,
        memory_repo=repo,
    )
    tracker._queue_outcome_for_batch = AsyncMock(return_value=None)
    return tracker, repo, telemetry


@pytest.mark.asyncio
async def test_agg_trade_rest_paths_accept_dict_rows() -> None:
    market_data = BinanceFuturesMarketData.__new__(BinanceFuturesMarketData)

    async def fake_call_public_http_json(*args, **kwargs):
        return [
            {"a": 11, "p": "100.5", "q": "2.0", "T": 1_000, "m": False},
            {"a": 12, "p": "100.0", "q": "1.0", "T": 1_050, "m": True},
        ]

    market_data._call_public_http_json = fake_call_public_http_json

    snapshot = await market_data._fetch_agg_trade_snapshot_rest("BTCUSDT", limit=2)
    trades, complete = await market_data.fetch_agg_trades(
        "BTCUSDT",
        start_time_ms=900,
        end_time_ms=1_100,
        page_limit=3,
        page_size=100,
    )

    assert snapshot.trade_count == 2
    assert snapshot.buy_qty == pytest.approx(2.0)
    assert snapshot.sell_qty == pytest.approx(1.0)
    assert [trade.trade_id for trade in trades] == [11, 12]
    assert complete is True


def test_signal_entry_mid_remains_raw_when_mark_price_is_close() -> None:
    signal = Signal(
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        score=0.8,
        timeframe="15m",
        entry_low=100.0,
        entry_high=100.4,
        stop=99.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        mark_price=100.21,
    )

    assert signal.entry_mid_raw == pytest.approx(100.2)
    assert signal.entry_mid == pytest.approx(100.2)
    assert signal.entry_reference_price == pytest.approx(100.21)


@pytest.mark.asyncio
async def test_tracking_expiry_falls_back_to_time_only_when_market_data_unavailable() -> (
    None
):
    class MarketDataStub:
        async def fetch_agg_trades(self, *args, **kwargs):
            raise MarketDataUnavailable(
                operation="agg", detail="offline", symbol="BTCUSDT"
            )

        async def fetch_klines(self, *args, **kwargs):
            raise MarketDataUnavailable(
                operation="klines", detail="offline", symbol="BTCUSDT"
            )

    tracker, repo, _ = make_tracker(MarketDataStub())
    now = datetime.now(UTC)
    tracked = make_tracked_state(
        created_at=now - timedelta(minutes=30),
        pending_expires_at=now - timedelta(minutes=5),
        active_expires_at=now + timedelta(minutes=30),
    )
    repo.active_rows = [tracker._tracked_to_payload(tracked)]

    events = await tracker.review_open_signals_for_symbol("BTCUSDT", dry_run=False)
    await asyncio.sleep(0)

    assert [event.event_type for event in events] == ["expired"]
    assert repo.active_rows[0]["status"] == "closed"
    assert repo.active_rows[0]["close_reason"] == "expired"


@pytest.mark.asyncio
async def test_short_price_tick_can_hit_tp2() -> None:
    tracker, _, _ = make_tracker(SimpleNamespace())
    tracked = make_tracked_state(
        direction="short",
        status="active",
        activated_at=datetime.now(UTC) - timedelta(minutes=1),
        stop=105.0,
        tp1=95.0,
        tp2=90.0,
    )

    events, closed = await tracker._apply_price_tick(
        tracked,
        price=89.0,
        occurred_at=datetime.now(UTC),
        precision_mode="trade",
    )
    await asyncio.sleep(0)

    assert closed is True
    assert [event.event_type for event in events] == ["tp1_hit", "tp2_hit"]
    assert tracked.close_reason == "tp2_hit"


@pytest.mark.asyncio
async def test_smart_exit_keeps_distinct_adaptive_outcome() -> None:
    tracker, repo, _ = make_tracker(SimpleNamespace())
    tracked = make_tracked_state(
        status="active",
        activated_at=datetime.now(UTC) - timedelta(minutes=2),
    )

    await tracker._close_event(
        tracked,
        event_type="smart_exit",
        occurred_at=datetime.now(UTC),
        price=101.0,
        precision_mode="system",
    )
    await asyncio.sleep(0)

    assert repo.setup_outcomes[-1] == ("ema_bounce", "smart_exit")


@pytest.mark.asyncio
async def test_select_and_deliver_uses_tracking_id_for_message_binding_and_feature_snapshot() -> (
    None
):
    signal = make_signal(created_at=datetime(2026, 4, 23, 0, 0, tzinfo=UTC))
    prepared = make_prepared(symbol=signal.symbol)
    feature_calls: list[tuple[str, object]] = []

    async def wait_noncritical(*, label: str, timeout: float, operation):
        return True, await operation

    async def _set_signal_features_async(tracking_id, features):
        feature_calls.append((tracking_id, features))

    tracker = SimpleNamespace(
        set_signal_features_async=AsyncMock(side_effect=_set_signal_features_async),
        arm_signals_with_messages=AsyncMock(return_value=None),
    )
    delivery_result = SimpleNamespace(
        signal=signal, status="sent", message_id=777, reason=None
    )
    bot = SignalBot.__new__(SignalBot)
    bot.settings = make_runtime_settings()
    bot._modern_repo = SimpleNamespace(
        is_symbol_blacklisted=AsyncMock(return_value=False),
        get_consecutive_sl=AsyncMock(return_value=0),
        get_active_signals=AsyncMock(return_value=[]),
        is_cooldown_active=AsyncMock(return_value=False),
        set_cooldown=AsyncMock(return_value=None),
        get_market_context=AsyncMock(return_value={}),
    )
    bot._wait_noncritical = wait_noncritical
    bot.delivery = SimpleNamespace(
        deliver=AsyncMock(return_value=[delivery_result]),
        send_analytics_companion=AsyncMock(return_value=None),
    )
    bot.tracker = tracker
    bot.telemetry = TelemetryStub()
    bot.alerts = SimpleNamespace(on_confirmed_signals=AsyncMock(return_value=None))
    bot._sync_ws_tracked_symbols = AsyncMock(return_value=None)
    bot._close_superseded_signal = AsyncMock(return_value=None)
    bot._deliver_tracking = AsyncMock(return_value=None)

    delivered, rejected, counts = await bot._select_and_deliver(
        [signal],
        prepared_by_tracking_id={signal.tracking_id: prepared},
    )

    assert delivered == [signal]
    assert rejected == []
    assert counts["sent"] == 1
    assert feature_calls
    assert feature_calls[0][0] == signal.tracking_id
    assert feature_calls[0][1].rsi_15m == pytest.approx(55.0)
    tracker.arm_signals_with_messages.assert_awaited_once()
    assert tracker.arm_signals_with_messages.await_args.kwargs["message_ids"] == {
        signal.tracking_id: 777
    }


@pytest.mark.asyncio
async def test_select_and_deliver_for_symbol_does_not_double_write_reject_telemetry() -> (
    None
):
    signal = make_signal(created_at=datetime(2026, 4, 23, 0, 1, tzinfo=UTC))
    bot = SignalBot.__new__(SignalBot)
    bot.settings = make_runtime_settings()
    bot.telemetry = TelemetryStub()
    bot._select_and_rank = Mock(return_value=[signal])
    bot._select_and_deliver = AsyncMock(
        return_value=([], [{"reason": "symbol_has_open_signal"}], Counter())
    )

    result = PipelineResult(
        symbol="BTCUSDT",
        trigger="kline_close",
        event_ts=datetime.now(UTC),
        raw_setups=1,
        candidates=[signal],
        rejected=[],
        prepared=None,
        funnel={},
    )

    candidates, rejected, delivered = await bot._select_and_deliver_for_symbol(
        "BTCUSDT", result
    )

    assert candidates == [signal]
    assert delivered == []
    assert rejected == [{"reason": "symbol_has_open_signal"}]
    assert bot.telemetry.rows == []


def test_swing_points_can_include_unconfirmed_tail() -> None:
    work = pl.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            "low": [9.5, 9.4, 9.3, 9.2, 9.1, 9.0],
        }
    )

    sh_confirmed, sl_confirmed = _swing_points(work, n=2)
    sh_tail, sl_tail = _swing_points(work, n=2, include_unconfirmed_tail=True)

    assert sh_confirmed[-1] is False
    assert sl_confirmed[-1] is False
    assert sh_tail[-1] is True
    assert sl_tail[-1] is True


def test_ml_filter_converts_polars_input_before_predict_proba() -> None:
    class _DummyModel:
        def predict_proba(self, x):
            assert x.__class__.__module__.startswith(("pandas", "numpy"))
            return [[0.2, 0.8]]

    ml = MLFilter.__new__(MLFilter)
    ml.enabled = True
    ml._model = _DummyModel()
    ml._model_metadata = {"trained_at": "test"}
    ml._feature_columns = None
    ml._extract_features = lambda signal, prepared: {"score": 0.75, "atr_pct": 1.1}

    result = ml.predict(make_signal(), SimpleNamespace())
    assert result.error is None
    assert result.probability == pytest.approx(0.8, rel=1e-4)


def test_funding_reversal_defaults_cover_runtime_params() -> None:
    defaults = FundingReversalSetup().get_optimizable_params()
    assert "funding_trend_bars" in defaults
    assert "min_delta_threshold" in defaults
    assert "sl_buffer_atr" in defaults


def test_funding_reversal_runtime_params_gate_delta_and_stop() -> None:
    settings = SimpleNamespace(
        filters=SimpleNamespace(
            setups={
                "funding_reversal": {
                    "min_delta_threshold": 0.15,
                    "sl_buffer_atr": 1.0,
                    "funding_trend_bars": 2.0,
                }
            }
        )
    )
    prepared = make_prepared(price=100.0)
    prepared.settings = settings
    prepared.funding_trend = "rising"
    prepared.work_15m = make_feature_frame(
        [103.0, 101.0, 100.0, 99.0, 100.0],
        opens=[102.0, 102.0, 101.0, 100.0, 104.0],
        highs=[104.0, 103.0, 102.0, 101.0, 110.0],
        lows=[85.0, 84.0, 82.0, 80.0, 99.0],
        delta_ratios=[0.55, 0.53, 0.52, 0.50, 0.45],
        volume_ratios=[1.3] * 5,
    )
    setup = FundingReversalSetup()

    assert setup.detect(prepared, settings) is None

    prepared.work_15m = make_feature_frame(
        [103.0, 101.0, 100.0, 99.0, 100.0],
        opens=[102.0, 102.0, 101.0, 100.0, 104.0],
        highs=[104.0, 103.0, 102.0, 101.0, 110.0],
        lows=[85.0, 84.0, 82.0, 80.0, 99.0],
        delta_ratios=[0.55, 0.53, 0.52, 0.50, 0.20],
        volume_ratios=[1.3] * 5,
    )

    signal = setup.detect(prepared, settings)
    assert signal is not None
    assert signal.stop == pytest.approx(112.0)


def test_cvd_divergence_respects_min_delta_threshold() -> None:
    settings = SimpleNamespace(
        filters=SimpleNamespace(
            setups={
                "cvd_divergence": {"min_delta_threshold": 0.2, "sl_buffer_atr": 0.5}
            }
        )
    )
    prepared = make_prepared(price=105.0)
    prepared.settings = settings
    prepared.bias_1h = "neutral"
    prepared.work_15m = make_feature_frame(
        [
            95.0,
            96.0,
            97.0,
            98.0,
            99.0,
            99.5,
            100.0,
            99.8,
            99.9,
            100.0,
            100.5,
            101.0,
            101.5,
            102.0,
            102.5,
            103.0,
            103.5,
            104.0,
            104.5,
            105.0,
        ],
        delta_ratios=[0.72] * 15 + [0.62] * 5,
        volume_ratios=[1.3] * 20,
    )
    setup = CVDDivergenceSetup()

    assert setup.detect(prepared, settings) is None

    prepared.work_15m = make_feature_frame(
        [
            95.0,
            96.0,
            97.0,
            98.0,
            99.0,
            99.5,
            100.0,
            99.8,
            99.9,
            100.0,
            100.5,
            101.0,
            101.5,
            102.0,
            102.5,
            103.0,
            103.5,
            104.0,
            104.5,
            105.0,
        ],
        delta_ratios=[0.72] * 15 + [0.32] * 5,
        volume_ratios=[1.3] * 20,
    )

    signal = setup.detect(prepared, settings)
    assert signal is not None
    assert signal.direction == "short"
    assert signal.stop == pytest.approx(107.0)


def test_hidden_divergence_respects_rsi_and_delta_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot.strategies.hidden_divergence as hidden_divergence_module

    def fake_swing_points(
        frame: pl.DataFrame, n: int, include_unconfirmed_tail: bool = True
    ) -> tuple[pl.Series, pl.Series]:
        assert n == 4
        size = frame.height
        sh = [False] * size
        sl = [False] * size
        sh[10] = True
        sl[5] = True
        sl[15] = True
        return pl.Series("sh", sh, dtype=pl.Boolean), pl.Series(
            "sl", sl, dtype=pl.Boolean
        )

    monkeypatch.setattr(hidden_divergence_module, "_swing_points", fake_swing_points)
    settings = SimpleNamespace(
        filters=SimpleNamespace(
            setups={
                "hidden_divergence": {
                    "rsi_divergence_lookback": 4.0,
                    "rsi_divergence_threshold": 6.0,
                    "min_delta_threshold": 0.1,
                    "sl_buffer_atr": 0.75,
                }
            }
        )
    )
    prepared = make_prepared(price=100.0)
    prepared.settings = settings
    prepared.bias_1h = "uptrend"
    prepared.work_1h = make_feature_frame(
        [100.0 + (idx * 0.2) for idx in range(20)],
        highs=[
            101.0,
            102.0,
            103.0,
            104.0,
            103.0,
            102.0,
            103.0,
            104.0,
            105.0,
            106.0,
            110.0,
            107.0,
            106.0,
            105.0,
            104.0,
            103.0,
            104.0,
            105.0,
            106.0,
            107.0,
        ],
        lows=[
            98.0,
            97.5,
            97.0,
            96.5,
            95.0,
            94.0,
            95.5,
            96.0,
            96.5,
            97.0,
            98.0,
            98.5,
            99.0,
            98.0,
            97.0,
            96.0,
            97.0,
            98.0,
            99.0,
            100.0,
        ],
        rsi_values=[
            50.0,
            49.0,
            48.0,
            47.0,
            45.0,
            40.0,
            44.0,
            46.0,
            48.0,
            50.0,
            55.0,
            54.0,
            53.0,
            52.0,
            50.0,
            33.0,
            45.0,
            47.0,
            49.0,
            51.0,
        ],
    )
    prepared.work_15m = make_feature_frame(
        [99.0, 99.5, 100.0, 100.5, 100.0],
        volume_ratios=[1.2] * 5,
        delta_ratios=[0.52, 0.53, 0.54, 0.55, 0.55],
    )
    setup = HiddenDivergenceSetup()

    assert setup.detect(prepared, settings) is None

    prepared.work_15m = make_feature_frame(
        [99.0, 99.5, 100.0, 100.5, 100.0],
        volume_ratios=[1.2] * 5,
        delta_ratios=[0.52, 0.53, 0.54, 0.55, 0.70],
    )

    signal = setup.detect(prepared, settings)
    assert signal is not None
    assert signal.direction == "long"
    assert signal.stop == pytest.approx(94.5)


def test_squeeze_setup_runtime_params_drive_breakout_and_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot.features as features_module

    def fake_swing_points(
        frame: pl.DataFrame, n: int, include_unconfirmed_tail: bool = False
    ) -> tuple[pl.Series, pl.Series]:
        size = frame.height
        sh = [False] * size
        sl = [False] * size
        sl[10] = True
        return pl.Series("sh", sh, dtype=pl.Boolean), pl.Series(
            "sl", sl, dtype=pl.Boolean
        )

    monkeypatch.setattr(features_module, "_swing_points", fake_swing_points)
    closes = [100.0 + (idx * 0.2) for idx in range(29)] + [97.0]
    highs = [104.0] * 19 + [106.0] * 10 + [100.0]
    lows = [92.0] * 10 + [80.0] + [93.0] * 19
    bb_widths = [0.03] * 29 + [0.018]
    bb_pct_bs = [0.5] * 29 + [0.15]
    kc_uppers = [c + 0.5 for c in closes]
    kc_lowers = [c - 0.5 for c in closes[:-1]] + [98.0]
    prepared = make_prepared(price=97.0)
    prepared.work_15m = make_feature_frame(
        closes,
        highs=highs,
        lows=lows,
        volume_ratios=[1.25] * 30,
        bb_widths=bb_widths,
        bb_pct_bs=bb_pct_bs,
        kc_uppers=kc_uppers,
        kc_lowers=kc_lowers,
        rsi_values=[55.0] * 30,
    )
    prepared.funding_rate = 0.0006

    strict_settings = SimpleNamespace(
        filters=SimpleNamespace(
            setups={
                "squeeze_setup": {
                    "bb_pct_b_threshold": 0.9,
                    "min_bb_compression_width": 0.02,
                    "volume_threshold": 1.2,
                    "sl_buffer_atr": 0.5,
                }
            }
        )
    )
    prepared.settings = strict_settings
    setup = SqueezeSetup()
    assert setup.detect(prepared, strict_settings) is None

    relaxed_settings = SimpleNamespace(
        filters=SimpleNamespace(
            setups={
                "squeeze_setup": {
                    "bb_pct_b_threshold": 0.8,
                    "min_bb_compression_width": 0.02,
                    "volume_threshold": 1.2,
                    "sl_buffer_atr": 0.5,
                }
            }
        )
    )
    prepared.settings = relaxed_settings

    signal = setup.detect(prepared, relaxed_settings)
    assert signal is not None
    assert signal.direction == "short"
    assert signal.stop == pytest.approx(107.0)


def test_shortlist_service_uses_book_ticker_age_contract() -> None:
    class WSStub:
        def get_ticker_age_seconds(self, symbol: str) -> float | None:
            return None

        def get_mark_price_snapshot(self, symbol: str) -> dict[str, float]:
            return {}

        def get_mark_price_age_seconds(self, symbol: str) -> float | None:
            return None

        def get_book_snapshot(self, symbol: str) -> tuple[float, float]:
            return 99.0, 101.0

        def get_book_ticker_age_seconds(self, symbol: str) -> float | None:
            return 0.25

    service = ShortlistService(
        SimpleNamespace(_ws_manager=WSStub(), client=SimpleNamespace())
    )
    enriched = service._enrich_shortlist_rows([{"symbol": "BTCUSDT"}])

    assert enriched[0]["book_age_seconds"] == pytest.approx(0.25)


def test_build_structural_targets_prefers_nearest_long_resistance() -> None:
    work_1h = pl.DataFrame(
        {
            "time": [
                datetime(2026, 4, 23, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 23, 1, 0, tzinfo=UTC),
                datetime(2026, 4, 23, 2, 0, tzinfo=UTC),
            ],
            "high": [180.0, 120.0, 150.0],
            "low": [90.0, 95.0, 98.0],
        }
    )
    sh_mask = pl.Series("sh", [True, True, True], dtype=pl.Boolean)

    stop, tp1, tp2 = build_structural_targets(
        direction="long",
        price_anchor=100.0,
        stop_basis=95.0,
        atr=2.0,
        work_1h=work_1h,
        sh_mask=sh_mask,
    )

    assert stop == pytest.approx(92.0)
    assert tp1 == pytest.approx(120.0)
    assert tp2 == pytest.approx(120.0)


def test_build_structural_targets_short_uses_resistance_above_entry_for_stop_anchor() -> (
    None
):
    work_1h = pl.DataFrame(
        {
            "time": [
                datetime(2026, 4, 23, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 23, 1, 0, tzinfo=UTC),
                datetime(2026, 4, 23, 2, 0, tzinfo=UTC),
            ],
            "high": [96.0, 103.0, 108.0],
            "low": [92.0, 90.0, 88.0],
        }
    )
    sh_mask = pl.Series("sh", [True, True, True], dtype=pl.Boolean)
    sl_mask = pl.Series("sl", [True, True, True], dtype=pl.Boolean)

    stop, tp1, tp2 = build_structural_targets(
        direction="short",
        price_anchor=100.0,
        stop_basis=95.0,
        atr=1.0,
        work_1h=work_1h,
        sh_mask=sh_mask,
        sl_mask=sl_mask,
    )

    assert stop == pytest.approx(104.5)
    assert tp1 == pytest.approx(92.0)
    assert tp2 == pytest.approx(92.0)


def test_crowd_position_respects_strategy_family() -> None:
    prepared = make_prepared(price=100.0)
    prepared.top_account_ls_ratio = 1.18
    prepared.top_position_ls_ratio = 1.12
    prepared.global_account_ls_ratio = 1.05
    prepared.global_ls_ratio = 1.05
    prepared.top_vs_global_ls_gap = 0.13
    prepared.taker_ratio = 1.15
    prepared.data_freshness_flags = ()

    continuation_signal = Signal(
        symbol="BTCUSDT",
        setup_id="structure_pullback",
        direction="long",
        score=0.8,
        timeframe="15m",
        entry_low=99.5,
        entry_high=100.5,
        stop=98.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
        strategy_family="continuation",
        confirmation_profile="trend_follow",
    )
    reversal_signal = Signal(
        symbol="BTCUSDT",
        setup_id="wick_trap_reversal",
        direction="long",
        score=0.8,
        timeframe="15m",
        entry_low=99.5,
        entry_high=100.5,
        stop=98.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
        strategy_family="reversal",
        confirmation_profile="countertrend_exhaustion",
    )

    continuation_score = _crowd_position(
        prepared, continuation_signal, SimpleNamespace()
    )
    reversal_score = _crowd_position(prepared, reversal_signal, SimpleNamespace())

    assert continuation_score > reversal_score
    assert continuation_score > 0.55
    assert reversal_score < 0.5


def test_wick_trap_params_keep_backward_compatible_alias() -> None:
    setup = WickTrapReversalSetup()
    settings = SimpleNamespace(
        filters=SimpleNamespace(
            setups={
                "wick_trap_reversal": {
                    "wick_atr_threshold": 0.77,
                }
            }
        )
    )

    params = setup.get_optimizable_params(settings)

    assert params["wick_through_atr_mult"] == pytest.approx(0.77)
    assert params["wick_atr_threshold"] == pytest.approx(0.77)


def test_symbol_analyzer_does_not_hide_unexpected_frame_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _DummyBinance:
        async def fetch_klines_cached(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        symbol_analyzer_module, "BinanceFuturesMarketData", _DummyBinance
    )

    bot = SimpleNamespace(
        client=_DummyBinance(),
        _ws_manager=None,
        settings=SimpleNamespace(
            filters=SimpleNamespace(min_bars_15m=210, min_bars_1h=210, min_bars_4h=210),
            runtime=SimpleNamespace(history_fetch_limit=120),
        ),
    )
    analyzer = SymbolAnalyzer(bot)

    async def _run() -> None:
        with caplog.at_level("ERROR", logger="bot.application.bot"):
            with pytest.raises(RuntimeError, match="boom"):
                await analyzer.fetch_frames(make_universe_symbol("BTCUSDT"))

    asyncio.run(_run())
    assert any(
        "unexpected frame fetch failure for BTCUSDT" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_shortlist_refresh_prefers_ws_light_between_full_rebalances() -> None:
    from bot.application.shortlist_service import ShortlistService

    shortlist = [make_universe_symbol(symbol="BTCUSDT")]
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            universe=SimpleNamespace(
                quote_asset="USDT",
                pinned_symbols=("BTCUSDT",),
                light_refresh_interval_seconds=60,
                full_refresh_interval_seconds=7200,
            ),
            runtime=SimpleNamespace(shortlist_refresh_interval_seconds=7200),
        ),
        _shortlist_lock=asyncio.Lock(),
        _shortlist=[],
        _shortlist_source="",
        _last_live_shortlist=[],
        _symbol_meta_by_symbol={
            "BTCUSDT": SimpleNamespace(
                symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"
            )
        },
        _last_shortlist_full_refresh_at=datetime.now(UTC),
        telemetry=TelemetryStub(),
    )
    service = ShortlistService(bot)
    service.build_light_shortlist = AsyncMock(
        return_value=(
            shortlist,
            {
                "mode": "ws_light",
                "eligible": 1,
                "dynamic_pool": 1,
                "pinned": 0,
                "avg_score": 0.8,
            },
        )
    )
    service.build_live_shortlist = AsyncMock(
        side_effect=AssertionError("full refresh should not run")
    )

    refreshed = await service.do_refresh_shortlist()

    assert refreshed == shortlist
    assert bot._shortlist_source == "ws_light"
    service.build_light_shortlist.assert_awaited_once()
    shortlist_row = bot.telemetry.rows[-1][1]
    assert shortlist_row["source_before"] == ""
    assert shortlist_row["source_after"] == "ws_light"
    assert shortlist_row["fallback_reason"] is None


def test_shortlist_fallback_reason_dictionary_is_fixed() -> None:
    assert SHORTLIST_FALLBACK_REASONS == {
        FALLBACK_REASON_WS_CACHE_COLD: "ws light cache not ready or missing symbol metadata",
        FALLBACK_REASON_FULL_REFRESH_DUE: "full refresh interval reached",
        FALLBACK_REASON_REFRESH_EXCEPTION: "shortlist refresh raised exception",
        FALLBACK_REASON_USING_CACHED: "reuse last live shortlist snapshot",
        FALLBACK_REASON_USING_PINNED: "fallback to configured pinned shortlist",
    }


@pytest.mark.asyncio
async def test_shortlist_refresh_sets_ws_cache_cold_fallback_reason() -> None:
    shortlist = [make_universe_symbol(symbol="ETHUSDT")]
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            universe=SimpleNamespace(
                quote_asset="USDT",
                pinned_symbols=("BTCUSDT",),
                light_refresh_interval_seconds=60,
                full_refresh_interval_seconds=7200,
            ),
            runtime=SimpleNamespace(shortlist_refresh_interval_seconds=7200),
        ),
        _shortlist_lock=asyncio.Lock(),
        _shortlist=[],
        _shortlist_source="ws_light",
        _last_live_shortlist=[],
        _symbol_meta_by_symbol={},
        _last_shortlist_full_refresh_at=datetime.now(UTC),
        client=SimpleNamespace(_exchange_info_cache=None),
        telemetry=TelemetryStub(),
    )
    service = ShortlistService(bot)
    service.build_light_shortlist = AsyncMock(return_value=([], {"mode": "ws_light"}))
    service.build_live_shortlist = AsyncMock(return_value=(shortlist, {"mode": "rest_full"}))

    await service.do_refresh_shortlist()

    shortlist_row = bot.telemetry.rows[-1][1]
    assert shortlist_row["source"] == "rest_full"
    assert shortlist_row["fallback_reason"] == FALLBACK_REASON_WS_CACHE_COLD


@pytest.mark.asyncio
async def test_shortlist_refresh_sets_full_refresh_due_reason() -> None:
    shortlist = [make_universe_symbol(symbol="BTCUSDT")]
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            universe=SimpleNamespace(
                quote_asset="USDT",
                pinned_symbols=("BTCUSDT",),
                light_refresh_interval_seconds=60,
                full_refresh_interval_seconds=1,
            ),
            runtime=SimpleNamespace(shortlist_refresh_interval_seconds=1),
        ),
        _shortlist_lock=asyncio.Lock(),
        _shortlist=[],
        _shortlist_source="ws_light",
        _last_live_shortlist=[],
        _symbol_meta_by_symbol={
            "BTCUSDT": SimpleNamespace(
                symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"
            )
        },
        _last_shortlist_full_refresh_at=datetime.now(UTC) - timedelta(seconds=60),
        telemetry=TelemetryStub(),
    )
    service = ShortlistService(bot)
    service.build_light_shortlist = AsyncMock(
        side_effect=AssertionError("ws light should be skipped when full refresh is due")
    )
    service.build_live_shortlist = AsyncMock(return_value=(shortlist, {"mode": "rest_full"}))

    await service.do_refresh_shortlist()

    shortlist_row = bot.telemetry.rows[-1][1]
    assert shortlist_row["source"] == "rest_full"
    assert shortlist_row["fallback_reason"] == FALLBACK_REASON_FULL_REFRESH_DUE


@pytest.mark.asyncio
async def test_shortlist_refresh_sets_using_cached_reason_and_cache_age() -> None:
    cached_shortlist = [make_universe_symbol(symbol="BTCUSDT")]
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            universe=SimpleNamespace(
                quote_asset="USDT",
                pinned_symbols=("BTCUSDT",),
                light_refresh_interval_seconds=60,
                full_refresh_interval_seconds=7200,
            ),
            runtime=SimpleNamespace(shortlist_refresh_interval_seconds=7200),
        ),
        _shortlist_lock=asyncio.Lock(),
        _shortlist=[],
        _shortlist_source="rest_full",
        _last_live_shortlist=list(cached_shortlist),
        _last_live_shortlist_at=datetime.now(UTC) - timedelta(seconds=42),
        _symbol_meta_by_symbol={
            "BTCUSDT": SimpleNamespace(
                symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"
            )
        },
        _last_shortlist_full_refresh_at=datetime.now(UTC),
        telemetry=TelemetryStub(),
    )
    service = ShortlistService(bot)
    service.build_light_shortlist = AsyncMock(return_value=([], {"mode": "ws_light"}))
    service.build_live_shortlist = AsyncMock(return_value=([], {"mode": "rest_full"}))

    await service.do_refresh_shortlist()

    shortlist_row = bot.telemetry.rows[-1][1]
    assert shortlist_row["source"] == "cached"
    assert shortlist_row["fallback_reason"] == FALLBACK_REASON_USING_CACHED
    assert shortlist_row["cached_shortlist_age_s"] is not None
    assert shortlist_row["cached_shortlist_age_s"] >= 40


@pytest.mark.asyncio
async def test_shortlist_refresh_sets_refresh_exception_reason_when_cache_exists() -> None:
    cached_shortlist = [make_universe_symbol(symbol="BTCUSDT")]
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            universe=SimpleNamespace(
                quote_asset="USDT",
                pinned_symbols=("BTCUSDT",),
                light_refresh_interval_seconds=60,
                full_refresh_interval_seconds=7200,
            ),
            runtime=SimpleNamespace(shortlist_refresh_interval_seconds=7200),
        ),
        _shortlist_lock=asyncio.Lock(),
        _shortlist=[],
        _shortlist_source="rest_full",
        _last_live_shortlist=list(cached_shortlist),
        _last_live_shortlist_at=datetime.now(UTC) - timedelta(seconds=10),
        _symbol_meta_by_symbol={},
        _last_shortlist_full_refresh_at=datetime.now(UTC),
        client=SimpleNamespace(_exchange_info_cache=None),
        telemetry=TelemetryStub(),
    )
    service = ShortlistService(bot)
    service.build_light_shortlist = AsyncMock(
        side_effect=RuntimeError("ws cache unavailable")
    )

    await service.do_refresh_shortlist()

    shortlist_row = bot.telemetry.rows[-1][1]
    assert shortlist_row["source"] == "cached"
    assert shortlist_row["fallback_reason"] == FALLBACK_REASON_REFRESH_EXCEPTION


@pytest.mark.asyncio
async def test_shortlist_refresh_sets_using_pinned_reason_when_no_cache() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            universe=SimpleNamespace(
                quote_asset="USDT",
                pinned_symbols=("BTCUSDT",),
                light_refresh_interval_seconds=60,
                full_refresh_interval_seconds=7200,
            ),
            runtime=SimpleNamespace(shortlist_refresh_interval_seconds=7200),
        ),
        _shortlist_lock=asyncio.Lock(),
        _shortlist=[],
        _shortlist_source="startup",
        _last_live_shortlist=[],
        _symbol_meta_by_symbol={},
        _last_shortlist_full_refresh_at=datetime.now(UTC),
        client=SimpleNamespace(_exchange_info_cache=None),
        telemetry=TelemetryStub(),
    )
    service = ShortlistService(bot)
    service.build_light_shortlist = AsyncMock(
        side_effect=RuntimeError("ws cache unavailable")
    )

    await service.do_refresh_shortlist()

    shortlist_row = bot.telemetry.rows[-1][1]
    assert shortlist_row["source"] == "pinned_fallback"
    assert shortlist_row["fallback_reason"] == FALLBACK_REASON_USING_PINNED


def test_build_pinned_shortlist_resolves_assets_from_meta_and_safe_quote_parsing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = SignalBot.__new__(SignalBot)
    bot.settings = SimpleNamespace(
        universe=SimpleNamespace(
            pinned_symbols=["BTCUSDT", "ETHUSDT", "1000PEPEUSDT", "DOGEXUSD", "BROKEN"],
            quote_asset="XUSD",
        )
    )
    bot.client = SimpleNamespace(_exchange_info_cache=None)
    bot._symbol_meta_by_symbol = {
        "BTCUSDT": SimpleNamespace(base_asset="BTC", quote_asset="USDT"),
        "ETHUSDT": SimpleNamespace(base_asset="ETH", quote_asset="USDT"),
        "1000PEPEUSDT": SimpleNamespace(base_asset="1000PEPE", quote_asset="USDT"),
    }

    with caplog.at_level("WARNING", logger="bot.application.bot"):
        shortlist = bot._build_pinned_shortlist()

    by_symbol = {row.symbol: row for row in shortlist}
    assert by_symbol["BTCUSDT"].base_asset == "BTC"
    assert by_symbol["BTCUSDT"].quote_asset == "USDT"
    assert by_symbol["ETHUSDT"].base_asset == "ETH"
    assert by_symbol["1000PEPEUSDT"].base_asset == "1000PEPE"
    assert by_symbol["DOGEXUSD"].base_asset == "DOGE"
    assert by_symbol["DOGEXUSD"].quote_asset == "XUSD"
    assert "BROKEN" not in by_symbol
    assert any(
        "unresolved base/quote assets" in rec.message and "BROKEN" in rec.message
        for rec in caplog.records
    )


def test_build_signal_normalizes_swapped_targets() -> None:
    prepared = make_prepared(price=100.0)

    signal = _build_signal(
        prepared=prepared,
        setup_id="breaker_block",
        direction="long",
        score=0.77,
        reasons=["test"],
        stop=95.0,
        tp1=112.0,
        tp2=108.0,
        price_anchor=100.0,
        atr=2.0,
    )

    assert signal is not None
    assert signal.take_profit_1 == pytest.approx(108.0)
    assert signal.take_profit_2 == pytest.approx(112.0)
    assert signal.target_integrity_status == "normalized"
    assert signal.single_target_mode is False


def test_build_signal_reads_adx14_and_preserves_zero_metrics() -> None:
    prepared = make_prepared(price=100.0)
    prepared.work_1h = prepared.work_1h.with_columns(pl.lit(0.0).alias("adx14"))
    prepared.work_15m = prepared.work_15m.with_columns(
        pl.lit(0.0).alias("volume_ratio20")
    )
    prepared.work_5m = pl.DataFrame(
        {
            "time": [datetime.now(UTC)],
            "close_time": [datetime.now(UTC)],
            "premium_zscore_5m": [0.0],
            "premium_slope_5m": [0.0],
            "ls_ratio": [0.0],
        }
    )

    signal = _build_signal(
        prepared=prepared,
        setup_id="breaker_block",
        direction="long",
        score=0.7,
        reasons=["test"],
        stop=95.0,
        tp1=108.0,
        tp2=112.0,
        price_anchor=100.0,
        atr=2.0,
    )

    assert signal is not None
    assert signal.adx_1h == pytest.approx(0.0)
    assert signal.volume_ratio == pytest.approx(0.0)
    assert signal.premium_zscore_5m == pytest.approx(0.0)
    assert signal.premium_slope_5m == pytest.approx(0.0)
    assert signal.ls_ratio == pytest.approx(0.0)


def test_ema_bounce_emits_1h_timeframe() -> None:
    setup = EmaBounceSetup()
    settings = SimpleNamespace(filters=SimpleNamespace(setups={}))

    t0 = datetime.now(UTC) - timedelta(hours=2)
    prepared = make_prepared(price=101.0)
    prepared.bias_1h = "uptrend"
    prepared.regime_1h_confirmed = "uptrend"
    prepared.work_1h = pl.DataFrame(
        {
            "time": [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)],
            "close_time": [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)],
            "open": [99.0, 99.4, 100.2],
            "high": [100.5, 100.8, 102.0],
            "low": [98.8, 99.1, 100.0],
            "close": [99.8, 100.0, 101.2],
            "volume": [900.0, 950.0, 1100.0],
            "atr14": [1.2, 1.3, 1.4],
            "rsi14": [52.0, 54.0, 56.0],
            "volume_ratio20": [1.0, 1.1, 1.3],
            "ema20": [99.6, 100.2, 100.5],
            "ema50": [99.0, 99.4, 99.8],
            "adx14": [21.0, 22.0, 24.0],
        }
    )

    signal = setup.detect(prepared, settings)

    assert signal is not None
    assert signal.timeframe == "1h"


@pytest.mark.parametrize(
    ("min_adx", "expect_signal"),
    [
        (20.0, True),
        (30.0, False),
    ],
)
def test_ema_bounce_config_min_adx_changes_outcome(
    min_adx: float, expect_signal: bool
) -> None:
    setup = EmaBounceSetup()
    settings = SimpleNamespace(
        filters=SimpleNamespace(setups={"ema_bounce": {"min_adx": min_adx}})
    )
    t0 = datetime.now(UTC) - timedelta(hours=2)
    prepared = make_prepared(price=101.0)
    prepared.bias_1h = "uptrend"
    prepared.regime_1h_confirmed = "uptrend"
    prepared.work_1h = pl.DataFrame(
        {
            "time": [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)],
            "close_time": [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)],
            "open": [98.5, 99.0, 99.6],
            "high": [99.8, 100.5, 101.2],
            "low": [98.2, 98.7, 99.4],
            "close": [99.2, 99.4, 100.1],
            "volume": [900.0, 950.0, 1100.0],
            "atr14": [1.2, 1.3, 1.4],
            "rsi14": [52.0, 54.0, 56.0],
            "volume_ratio20": [1.0, 1.1, 1.3],
            "ema20": [99.5, 100.0, 100.4],
            "ema50": [99.0, 99.3, 99.7],
            "adx14": [21.0, 22.0, 24.0],
        }
    )
    signal = setup.detect(prepared, settings)

    assert (signal is not None) is expect_signal


@pytest.mark.parametrize(
    ("min_trend_score", "expect_signal"),
    [
        (0.40, True),
        (0.95, False),
    ],
)
def test_structure_pullback_config_trend_threshold_changes_outcome(
    min_trend_score: float, expect_signal: bool
) -> None:
    setup = StructurePullbackSetup()
    settings = SimpleNamespace(
        filters=SimpleNamespace(
            min_adx_1h=18.0,
            setups={"structure_pullback": {"min_trend_score": min_trend_score}},
        )
    )
    prepared = make_prepared(price=106.0)
    prepared.bias_1h = "uptrend"
    prepared.regime_1h_confirmed = "uptrend"
    prepared.regime_4h_confirmed = "uptrend"
    prepared.structure_1h = "uptrend"
    now = datetime.now(UTC)
    prepared.work_1h = pl.DataFrame(
        {
            "time": [
                now - timedelta(hours=4),
                now - timedelta(hours=3),
                now - timedelta(hours=2),
                now - timedelta(hours=1),
                now,
            ],
            "close_time": [
                now - timedelta(hours=4),
                now - timedelta(hours=3),
                now - timedelta(hours=2),
                now - timedelta(hours=1),
                now,
            ],
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [102.0, 104.0, 106.0, 109.0, 112.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 103.0, 105.0, 107.0, 108.0],
            "volume": [900.0, 950.0, 1000.0, 1050.0, 1100.0],
            "atr14": [1.2, 1.3, 1.4, 1.5, 1.6],
            "rsi14": [50.0, 52.0, 54.0, 55.0, 56.0],
            "volume_ratio20": [1.0, 1.1, 1.2, 1.2, 1.3],
            "ema20": [100.5, 101.5, 102.5, 103.5, 104.5],
            "ema50": [99.0, 99.5, 100.0, 100.5, 101.0],
            "adx14": [24.0, 25.0, 26.0, 27.0, 28.0],
            "bb_pct_b": [0.50, 0.55, 0.60, 0.65, 0.70],
        }
    )
    prepared.work_15m = pl.DataFrame(
        {
            "time": [
                now - timedelta(minutes=60),
                now - timedelta(minutes=45),
                now - timedelta(minutes=30),
                now - timedelta(minutes=15),
                now,
            ],
            "close_time": [
                now - timedelta(minutes=60),
                now - timedelta(minutes=45),
                now - timedelta(minutes=30),
                now - timedelta(minutes=15),
                now,
            ],
            "open": [104.5, 104.2, 104.0, 103.8, 106.0],
            "high": [104.8, 104.3, 104.2, 104.0, 107.0],
            "low": [104.1, 103.9, 103.6, 103.2, 105.8],
            "close": [104.2, 104.0, 103.8, 103.5, 106.0],
            "volume": [800.0, 820.0, 850.0, 900.0, 1200.0],
            "atr14": [0.8, 0.8, 0.9, 1.0, 1.0],
            "rsi14": [48.0, 47.0, 45.0, 43.0, 55.0],
            "volume_ratio20": [0.9, 0.95, 1.0, 1.05, 1.2],
            "bb_pct_b": [0.40, 0.38, 0.36, 0.35, 0.60],
            "ema20": [104.0, 103.9, 103.8, 103.7, 103.9],
            "adx14": [20.0, 21.0, 22.0, 23.0, 24.0],
        }
    )
    prepared.work_4h = pl.DataFrame(
        {
            "time": [now - timedelta(hours=8), now - timedelta(hours=4), now],
            "close_time": [now - timedelta(hours=8), now - timedelta(hours=4), now],
            "open": [98.0, 102.0, 106.0],
            "high": [103.0, 109.0, 120.0],
            "low": [97.0, 101.0, 105.0],
            "close": [102.0, 106.0, 111.0],
            "volume": [5000.0, 5500.0, 6000.0],
            "atr14": [2.5, 2.6, 2.7],
            "rsi14": [52.0, 54.0, 56.0],
            "volume_ratio20": [1.0, 1.1, 1.2],
            "ema20": [100.0, 103.0, 106.0],
            "ema50": [95.0, 98.0, 101.0],
            "adx14": [24.0, 25.0, 26.0],
            "bb_pct_b": [0.55, 0.58, 0.62],
        }
    )
    signal = setup.detect(prepared, settings)

    assert (signal is not None) is expect_signal


@pytest.mark.parametrize(
    ("min_mitigation_pct", "expect_signal"),
    [
        (0.20, True),
        (0.70, False),
    ],
)
def test_fvg_config_mitigation_threshold_changes_outcome(
    min_mitigation_pct: float, expect_signal: bool
) -> None:
    setup = FVGSetup()
    settings = SimpleNamespace(
        filters=SimpleNamespace(
            setups={
                "fvg": {
                    "min_mitigation_pct": min_mitigation_pct,
                    "min_fvg_size_atr": 0.05,
                    "sl_buffer_atr": 0.5,
                }
            }
        )
    )
    now = datetime.now(UTC)
    prepared = make_prepared(price=100.0)
    prepared.bias_1h = "uptrend"
    prepared.regime_1h_confirmed = "uptrend"
    prepared.structure_1h = "uptrend"
    prepared.work_15m = pl.DataFrame(
        {
            "time": [
                now - timedelta(minutes=60),
                now - timedelta(minutes=45),
                now - timedelta(minutes=30),
                now - timedelta(minutes=15),
                now,
            ],
            "close_time": [
                now - timedelta(minutes=60),
                now - timedelta(minutes=45),
                now - timedelta(minutes=30),
                now - timedelta(minutes=15),
                now,
            ],
            "open": [98.2, 99.6, 99.8, 99.7, 100.0],
            "high": [98.6, 100.0, 100.0, 100.2, 100.3],
            "low": [98.0, 99.4, 99.9, 99.7, 99.9],
            "close": [98.4, 99.8, 100.0, 100.0, 100.0],
            "volume": [800.0, 950.0, 1100.0, 1050.0, 1000.0],
            "atr14": [1.0, 1.0, 1.0, 1.0, 1.0],
            "rsi14": [50.0, 52.0, 54.0, 55.0, 56.0],
            "volume_ratio20": [1.0, 1.1, 1.2, 1.1, 1.0],
            "ema20": [98.5, 99.0, 99.4, 99.7, 99.9],
            "ema50": [98.0, 98.5, 98.8, 99.0, 99.2],
            "adx14": [20.0, 21.0, 22.0, 23.0, 24.0],
        }
    )
    signal = setup.detect(prepared, settings)

    assert (signal is not None) is expect_signal


def test_load_settings_merges_legacy_strategy_overrides_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[bot]
[bot.filters]
[bot.filters.setups]
ema_bounce = { min_adx = 24.0 }
""".strip()
    )
    legacy_dir = tmp_path / "config" / "strategies"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "ema_bounce.toml").write_text(
        """
[detection]
ema_touch_tolerance_pct = 0.01
[filters]
min_adx = 18.0
""".strip()
    )
    monkeypatch.setenv("TG_TOKEN", "")
    monkeypatch.setenv("TARGET_CHAT_ID", "")

    settings = load_settings(config_file)
    overrides = settings.filters.setups["ema_bounce"]

    assert overrides["ema_touch_tolerance_pct"] == pytest.approx(0.01)
    assert overrides["min_adx"] == pytest.approx(24.0)


def test_runtime_validation_rejects_private_ws_routes(tmp_path: Path) -> None:
    settings = BotSettings(
        tg_token="",
        target_chat_id="",
        data_dir=tmp_path / "data",
        ws={"base_url": "wss://fstream.binance.com/private"},
    )

    with pytest.raises(ValueError, match="public market streams only"):
        settings.validate_for_runtime(require_telegram=False)


def test_ws_stream_endpoint_class_keeps_public_market_split() -> None:
    assert ws_subscriptions.stream_endpoint_class("btcusdt@bookTicker") == "public"
    assert ws_subscriptions.stream_endpoint_class("btcusdt@kline_15m") == "market"
    assert ws_subscriptions.stream_endpoint_class("btcusdt@aggTrade") == "market"
    assert ws_subscriptions.stream_endpoint_class("btcusdt@markPrice@1s") == "market"
    assert ws_subscriptions.stream_endpoint_class("!ticker@arr") == "market"

    with pytest.raises(
        ValueError, match="private/auth websocket streams are not allowed"
    ):
        ws_subscriptions.stream_endpoint_class("listenKey")


@pytest.mark.asyncio
async def test_single_target_price_tick_closes_once_without_tp2_event() -> None:
    tracker, repo, _ = make_tracker(SimpleNamespace())
    tracked = make_tracked_state(
        status="active",
        activated_at=datetime.now(UTC) - timedelta(minutes=1),
        stop=97.5,
        tp1=105.0,
        tp2=105.0,
        single_target_mode=True,
        target_integrity_status="single_target",
    )

    events, closed = await tracker._apply_price_tick(
        tracked,
        price=105.0,
        occurred_at=datetime.now(UTC),
        precision_mode="trade",
    )
    await asyncio.sleep(0)

    assert closed is True
    assert [event.event_type for event in events] == ["tp1_hit"]
    assert tracked.close_reason == "tp1_hit"
    assert repo.tracking_stats["tp1_hit"] == 1
    assert repo.tracking_stats["tp2_hit"] == 0


def test_family_confirmation_rejects_missing_fast_context_when_strict() -> None:
    bot = SignalBot.__new__(SignalBot)
    bot.settings = make_runtime_settings(strict_data_quality=True)
    prepared = make_prepared()
    prepared.work_5m = None
    prepared.mark_index_spread_bps = None
    prepared.depth_imbalance = None
    prepared.microprice_bias = None
    signal = make_signal()

    ok, reason, details = bot._check_family_confirmation(
        signal, prepared, metadata=None
    )

    assert ok is False
    assert reason == "data.fast_context_missing"
    assert details["fallback"] == "context_missing"


@pytest.mark.asyncio
async def test_engine_skip_result_keeps_setup_id_and_reason_code() -> None:
    class HistoryHungrySetup(BaseSetup):
        setup_id = "history_hungry"
        min_history_bars = 10

        def get_optimizable_params(self, settings=None) -> dict[str, float]:
            return {}

        def detect(self, prepared: PreparedSymbol, settings):
            raise AssertionError("detect should not run when can_calculate is false")

    registry = StrategyRegistry()
    settings = make_runtime_settings()
    registry.register(
        HistoryHungrySetup(SetupParams(enabled=True), settings), enabled=True
    )
    engine = SignalEngine(registry, settings)

    results = await engine.calculate_all(make_prepared())

    assert len(results) == 1
    result = results[0]
    assert result.setup_id == "history_hungry"
    assert result.decision is not None
    assert result.decision.is_skip
    assert result.decision.reason_code == "data.work_1h_insufficient_history"


@pytest.mark.asyncio
async def test_parallel_strategy_rejections_keep_distinct_reason_codes() -> None:
    class RejectASetup(BaseSetup):
        setup_id = "reject_a"
        min_history_bars = 1

        def get_optimizable_params(self, settings=None) -> dict[str, float]:
            return {}

        def detect(self, prepared: PreparedSymbol, settings):
            from bot.setups import _reject

            _reject(prepared, self.setup_id, "atr_invalid", atr=float("nan"))
            return None

    class RejectBSetup(BaseSetup):
        setup_id = "reject_b"
        min_history_bars = 1

        def get_optimizable_params(self, settings=None) -> dict[str, float]:
            return {}

        def detect(self, prepared: PreparedSymbol, settings):
            from bot.setups import _reject

            _reject(prepared, self.setup_id, "price_missing")
            return None

    registry = StrategyRegistry()
    settings = make_runtime_settings()
    registry.register(RejectASetup(SetupParams(enabled=True), settings), enabled=True)
    registry.register(RejectBSetup(SetupParams(enabled=True), settings), enabled=True)
    engine = SignalEngine(registry, settings)

    results = await engine.calculate_all(make_prepared())
    reason_by_setup = {
        result.setup_id: result.decision.reason_code
        for result in results
        if result.decision is not None
    }

    assert reason_by_setup["reject_a"] == "indicator.atr_invalid"
    assert reason_by_setup["reject_b"] == "data.price_missing"


@pytest.mark.asyncio
async def test_engine_calculate_all_runs_strategies_concurrently() -> None:
    class SlowSetupA(BaseSetup):
        setup_id = "slow_a"
        min_history_bars = 1

        def get_optimizable_params(self, settings=None) -> dict[str, float]:
            return {}

        def detect(self, prepared: PreparedSymbol, settings):
            time.sleep(0.15)
            return None

    class SlowSetupB(BaseSetup):
        setup_id = "slow_b"
        min_history_bars = 1

        def get_optimizable_params(self, settings=None) -> dict[str, float]:
            return {}

        def detect(self, prepared: PreparedSymbol, settings):
            time.sleep(0.15)
            return None

    registry = StrategyRegistry()
    settings = make_runtime_settings()
    settings.runtime.analysis_concurrency = 2
    registry.register(SlowSetupA(SetupParams(enabled=True), settings), enabled=True)
    registry.register(SlowSetupB(SetupParams(enabled=True), settings), enabled=True)
    engine = SignalEngine(registry, settings)

    started = time.perf_counter()
    results = await engine.calculate_all(make_prepared())
    elapsed = time.perf_counter() - started

    assert len(results) == 2
    assert elapsed < 0.33


@pytest.mark.asyncio
async def test_strategy_exception_surfaces_classified_error_decision() -> None:
    class BrokenSetup(BaseSetup):
        setup_id = "broken_setup"
        min_history_bars = 1

        def get_optimizable_params(self, settings=None) -> dict[str, float]:
            return {}

        def detect(self, prepared: PreparedSymbol, settings):
            raise ValueError("boom")

    registry = StrategyRegistry()
    settings = make_runtime_settings()
    registry.register(BrokenSetup(SetupParams(enabled=True), settings), enabled=True)
    engine = SignalEngine(registry, settings)

    results = await engine.calculate_all(make_prepared())

    assert len(results) == 1
    result = results[0]
    assert result.setup_id == "broken_setup"
    assert result.decision is not None
    assert result.decision.reason_code == "bug.error"
    assert result.decision.details["error_class"] == "bug"
    assert result.error == "boom"


def test_cli_stderr_prefilter_detects_logger_timestamp_prefix_for_any_year() -> None:
    assert _is_preformatted_log_stderr(
        "2026-04-23 10:11:12,345 | WARNING | stderr | write:1 | STDERR: loop warning"
    )
    assert _is_preformatted_log_stderr(
        "2027-01-01 00:00:00,001 | INFO    | bot.cli | run:42 | BOT SESSION STARTED"
    )
    assert not _is_preformatted_log_stderr("unstructured stderr noise from dependency")
