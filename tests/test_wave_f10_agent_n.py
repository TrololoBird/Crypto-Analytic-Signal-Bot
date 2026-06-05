"""Wave F10 Agent N — regime cache, btc_phase gate, global_market_regime, funding."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import polars as pl
import pytest

from bot.delivery.filters import apply_global_filters
from bot.delivery.scoring import ScoringResult
from bot.domain.config import BotSettings
from bot.domain.schemas import PreparedSymbol, Signal, UniverseSymbol
from bot.persistence.repository.memory import MemoryRepository
from bot.regime.composite_regime import (
    CompositeRegimeAnalyzer,
    benchmark_funding_median,
    build_minimal_regime_frame_4h,
)
from bot.regime.market import MarketRegimeAnalyzer
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator
from bot.runtime.symbol_analyzer import AnalyzerContextMixin


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _htf_frames(*, slope: float) -> tuple[pl.DataFrame, pl.DataFrame]:
    def frame(start: float, step: float) -> pl.DataFrame:
        prices = [start + step * idx for idx in range(60)]
        return pl.DataFrame(
            {
                "close": prices,
                "high": [p * 1.002 for p in prices],
                "low": [p * 0.998 for p in prices],
                "open": prices,
            }
        )

    return frame(120.0, slope), frame(130.0, slope * 0.5)


def _primary(
    *,
    close: float,
    ema20: float,
    ema50: float,
    rsi: float,
    volume: float = 150.0,
    base_volume: float = 100.0,
) -> pl.DataFrame:
    rows = 25
    volumes = [base_volume] * (rows - 1) + [volume]
    now = datetime.now(UTC)
    close_times = [now - timedelta(minutes=15 * (rows - 1 - idx)) for idx in range(rows)]
    return pl.DataFrame(
        {
            "close": [close] * rows,
            "ema20": [ema20] * rows,
            "ema50": [ema50] * rows,
            "rsi14": [rsi] * rows,
            "volume": volumes,
            "atr_pct": [1.2] * rows,
            "adx_1h": [22.0] * rows,
            "adx14": [22.0] * rows,
            "volume_ratio20": [1.1] * rows,
            "close_time": close_times,
        }
    )


def _prepared_gate(
    primary: pl.DataFrame,
    *,
    btc_phase: str | None = None,
    market_ctx: dict[str, object] | None = None,
) -> SimpleNamespace:
    h1, h4 = _htf_frames(slope=0.5)
    return SimpleNamespace(
        work_15m=primary,
        work_1h=h1,
        work_4h=h4,
        regime_1h_confirmed="ranging",
        regime_4h_confirmed="ranging",
        btc_phase=btc_phase,
        market_ctx=market_ctx,
        microprice_bias=0.08,
        agg_trade_delta_30s=0.02,
        funding_rate=0.0001,
        oi_change_pct=1.0,
    )


def _signal(**overrides: object) -> SimpleNamespace:
    base = {
        "direction": "long",
        "confirmation_profile": "countertrend_exhaustion",
        "btc_bias": "bear",
        "setup_id": "volume_climax_reversal",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- N6: MarketRegimeAnalyzer cache TTL + market_context age ---


def test_market_regime_cache_ttl_follows_intelligence_refresh() -> None:
    settings = _settings(intelligence={"refresh_interval_seconds": 600})
    analyzer = MarketRegimeAnalyzer(settings)
    assert analyzer._cache_ttl_seconds == 600.0


def test_market_regime_cache_age_tracks_last_update() -> None:
    settings = _settings()
    analyzer = MarketRegimeAnalyzer(settings)
    ticker = [{"symbol": "BTCUSDT", "price_change_percent": 1.0, "quote_volume": 1e9}]
    analyzer.analyze(ticker)
    assert analyzer.cache_age_seconds >= 0.0
    time.sleep(0.01)
    assert analyzer.cache_age_seconds > 0.0


def test_memory_repo_stamps_market_context_age_seconds() -> None:
    age = MemoryRepository._market_context_age_seconds(
        (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    )
    assert age is not None
    assert 44.0 <= age <= 50.0


# --- N5-lite: BTC+ETH funding median ---


def test_benchmark_funding_median_uses_btc_eth_not_first_dict_value() -> None:
    funding = {"SOLUSDT": 0.001, "BTCUSDT": 0.0002, "ETHUSDT": 0.0008}
    assert benchmark_funding_median(funding) == pytest.approx(0.0005)
    assert benchmark_funding_median(funding) != funding["SOLUSDT"]


def test_composite_analyzer_uses_benchmark_funding_median() -> None:
    analyzer = CompositeRegimeAnalyzer()
    funding = {"SOLUSDT": 0.002, "BTCUSDT": -0.0004, "ETHUSDT": 0.0004}
    result = analyzer.analyze([], funding, {"BTCUSDT": {"basis_pct": -0.01}})
    assert result.regime in {"bull", "bear", "ranging", "volatile"}


# --- N4-lite: minimal regime_frame_4h ---


def test_build_minimal_regime_frame_4h_columns() -> None:
    closes = [100.0 + idx * 0.5 for idx in range(30)]
    frame = build_minimal_regime_frame_4h(closes)
    assert frame is not None
    assert set(frame.columns) == {"log_returns", "realized_vol", "atr_pct"}
    assert frame.height == len(closes)


def test_composite_rule_based_uses_regime_frame_4h() -> None:
    closes = [100.0 + idx * 0.3 for idx in range(40)]
    history = build_minimal_regime_frame_4h(closes)
    assert history is not None
    analyzer = CompositeRegimeAnalyzer()
    result = analyzer.analyze(
        [],
        {"BTCUSDT": 0.0001, "ETHUSDT": 0.0001},
        {"BTCUSDT": {"regime_frame_4h": history, "basis_pct": 0.0}},
    )
    assert result.regime in {"bull", "bear", "ranging", "volatile"}


def test_composite_accepts_json_safe_regime_frame_dict() -> None:
    closes = [100.0 + idx * 0.2 for idx in range(25)]
    frame = build_minimal_regime_frame_4h(closes)
    assert frame is not None
    payload = {col: frame[col].to_list() for col in frame.columns}
    analyzer = CompositeRegimeAnalyzer()
    result = analyzer.analyze(
        [],
        {"BTCUSDT": 0.0},
        {"BTCUSDT": {"regime_frame_4h": payload, "basis_pct": 0.001}},
    )
    assert result.regime in {"bull", "bear", "ranging", "volatile"}


# --- N7: btc_phase in delivery gate details ---


def test_gate_details_include_btc_phase_rule_for_decline_countertrend() -> None:
    prepared = _prepared_gate(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0),
        btc_phase="decline",
    )
    signal = _signal(confirmation_profile="countertrend_exhaustion")
    _, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
    )
    assert details["btc_phase"] == "decline"
    assert details["btc_phase_rule"] == "countertrend_decline_penalty_eligible"


def test_gate_details_btc_phase_unknown_when_missing() -> None:
    prepared = _prepared_gate(_primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0))
    signal = _signal(confirmation_profile="trend_follow")
    _, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
    )
    assert details["btc_phase"] == "unknown"
    assert details["btc_phase_rule"] == "none"


# --- N7: optional score penalty for countertrend in decline ---


def _universe() -> UniverseSymbol:
    return UniverseSymbol(
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1e9,
        price_change_pct=1.0,
        last_price=100.0,
    )


def _filter_prepared(**overrides: object) -> PreparedSymbol:
    frame = _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0, volume=150.0)
    base = PreparedSymbol(
        universe=_universe(),
        work_1h=frame,
        work_15m=frame,
        work_4h=frame,
        work_primary=frame,
        bid_price=100.0,
        ask_price=100.1,
        spread_bps=5.0,
        btc_phase="decline",
        regime_1h_confirmed="uptrend",
        funding_rate=0.0001,
        oi_change_pct=1.0,
        settings=_settings(),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _filter_signal(**overrides: object) -> Signal:
    base = {
        "symbol": "ETHUSDT",
        "setup_id": "volume_climax_reversal",
        "direction": "long",
        "score": 0.80,
        "timeframe": "15m",
        "entry_low": 99.0,
        "entry_high": 101.0,
        "stop": 94.0,
        "take_profit_1": 112.0,
        "take_profit_2": 118.0,
        "strategy_family": "reversal",
        "confirmation_profile": "countertrend_exhaustion",
    }
    base.update(overrides)
    return Signal(**base)


def test_btc_decline_applies_countertrend_score_penalty() -> None:
    prepared = _filter_prepared()
    signal = _filter_signal(mark_price=100.0)
    confluence = MagicMock()
    scoring_result = ScoringResult(
        base_score=0.80,
        adjustments={},
        final_score=0.80,
        setup_id=signal.setup_id,
    )
    confluence.score.return_value = SimpleNamespace(
        final_score=0.80,
        to_scoring_result=lambda: scoring_result,
    )
    result = apply_global_filters(
        signal,
        prepared,
        settings=_settings(),
        confluence_engine=confluence,
    )
    assert result is not None
    passed, updated, _reason, _scoring, _details = result
    assert passed is True
    assert updated.score == pytest.approx(0.80 * 0.90)
    assert "btc_decline_penalty_applied" in updated.passed_filters


# --- N8: global_market_regime injection ---


@pytest.mark.asyncio
async def test_pipeline_injects_global_market_regime_from_db() -> None:

    frame = _primary(close=100.0, ema20=100.0, ema50=100.0, rsi=50.0)
    prepared = PreparedSymbol(
        universe=_universe(),
        work_1h=frame,
        work_15m=frame,
        bid_price=100.0,
        ask_price=100.1,
        spread_bps=5.0,
    )
    bot = MagicMock()
    bot._modern_repo.get_market_context = AsyncMock(
        return_value={
            "market_regime": "bear",
            "btc_bias": "downtrend",
            "eth_bias": "neutral",
            "btc_phase": "decline",
            "market_context_age_seconds": 12.5,
        }
    )
    mixin = AnalyzerContextMixin(bot)
    market_ctx = await bot._modern_repo.get_market_context()
    for key in ("btc_bias", "eth_bias", "btc_phase", "market_context_age_seconds"):
        value = market_ctx.get(key)
        if value is not None and hasattr(prepared, key):
            setattr(prepared, key, value)
    market_regime = market_ctx.get("market_regime")
    if market_regime is not None:
        prepared.global_market_regime = str(market_regime)
    prepared.market_ctx = {
        key: market_ctx[key]
        for key in (
            "btc_bias",
            "eth_bias",
            "market_regime",
            "macro_risk_mode",
            "btc_phase",
            "market_context_age_seconds",
        )
        if key in market_ctx
    }
    assert prepared.global_market_regime == "bear"
    assert prepared.market_context_age_seconds == pytest.approx(12.5)
    assert prepared.market_ctx["market_regime"] == "bear"
    assert mixin._bot is bot
