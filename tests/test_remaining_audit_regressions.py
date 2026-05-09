from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from bot.config import BotSettings
from bot.models import PreparedSymbol, UniverseSymbol
from bot.public_intelligence import PublicIntelligenceService
from bot.setup_base import SetupParams
from bot.setups import _compute_dynamic_score
from bot.setups.smc import SMCZone, fvg, latest_fvg_zone, liquidity_pools
from bot.strategies.roadmap import OIDivergenceSetup
from bot.strategies.liquidity_sweep import LiquiditySweepSetup
from bot.strategies.order_block import OrderBlockSetup
from bot.strategies.wick_trap_reversal import WickTrapReversalSetup


class _TelemetryStub:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def append_jsonl(self, filename: str, row: dict) -> None:
        self.rows.append((filename, row))


def _feature_frame(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    price: float = 100.0,
    atr: float = 2.0,
    rsi: float = 55.0,
) -> pl.DataFrame:
    count = len(closes)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    times = [start + timedelta(hours=idx) for idx in range(count)]
    opens = opens or list(closes)
    highs = highs or [max(open_, close) + 1.0 for open_, close in zip(opens, closes)]
    lows = lows or [min(open_, close) - 1.0 for open_, close in zip(opens, closes)]
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
            "rsi14": [rsi] * count,
            "adx14": [25.0] * count,
            "volume_ratio20": [1.4] * count,
            "macd_hist": [0.25] * count,
            "ema20": [price - 1.0] * count,
            "ema50": [price - 2.0] * count,
            "ema200": [price - 5.0] * count,
            "supertrend_dir": [1.0] * count,
            "obv_above_ema": [1.0] * count,
            "bb_pct_b": [0.5] * count,
            "bb_width": [0.018] * count,
            "kc_upper": [price + 1.0] * count,
            "kc_lower": [price - 1.0] * count,
            "delta_ratio": [0.5] * count,
        }
    )


def _prepared(
    frame: pl.DataFrame,
    *,
    settings: BotSettings,
    price: float = 100.0,
    bias_1h: str = "neutral",
    structure_1h: str = "ranging",
) -> PreparedSymbol:
    return PreparedSymbol(
        universe=UniverseSymbol(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            contract_type="PERPETUAL",
            status="TRADING",
            onboard_date_ms=0,
            quote_volume=1_000_000.0,
            price_change_pct=0.0,
            last_price=price,
        ),
        work_1h=frame,
        work_15m=frame,
        work_4h=frame,
        bid_price=price - 0.1,
        ask_price=price + 0.1,
        spread_bps=2.0,
        mark_price=price,
        bias_1h=bias_1h,
        structure_1h=structure_1h,
        settings=settings,
    )


def _snapshot() -> dict:
    return {
        "ts": datetime(2026, 1, 1, 12, tzinfo=UTC).isoformat(),
        "runtime_mode": "signal_only",
        "source_policy": "binance_only",
        "smart_exit_mode": "heuristic_v1",
        "gamma_semantics": "proxy_only",
        "policy": {},
        "confirmed_facts": [],
        "inferences": [],
        "assumptions": [],
        "uncertainty": [],
        "barrier": {},
        "macro": {},
        "options": {},
        "derivatives": {},
        "harmonic": {},
    }


@pytest.mark.asyncio
async def test_public_intelligence_report_writes_are_awaitable(tmp_path) -> None:
    settings = BotSettings(
        tg_token="0" * 30,
        target_chat_id="0",
        data_dir=tmp_path,
    )
    service = PublicIntelligenceService(
        settings,
        market_data=object(),
        telemetry=_TelemetryStub(),
    )

    await service._write_latest_snapshot(_snapshot())
    await service._maybe_write_hourly_report(_snapshot())

    reports_dir = tmp_path / "session" / "reports"
    assert (reports_dir / "latest_public_intelligence.json").exists()
    assert (reports_dir / "latest_public_intelligence.md").exists()
    assert (reports_dir / "hourly_public_intelligence_20260101_120000.json").exists()
    assert (reports_dir / "hourly_public_intelligence_20260101_120000.md").exists()


def test_liquidity_sweep_accepts_previous_bar_sweep_with_current_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    closes = [100.0] * 8 + [100.5, 100.2]
    highs = [101.0] * 8 + [102.0, 100.8]
    lows = [99.0] * 10
    frame = _feature_frame(
        closes,
        highs=highs,
        lows=lows,
        price=100.2,
        rsi=45.0,
    )

    def _latest_sweep(*_args, **_kwargs) -> SMCZone:
        return SMCZone(
            kind="liquidity_sweep",
            direction="short",
            top=101.0,
            bottom=101.0,
            created_index=8,
            state="mitigated",
            midpoint=101.0,
            width=0.0,
            level=101.0,
            sweep_index=8,
        )

    monkeypatch.setattr(
        "bot.strategies.liquidity_sweep.latest_liquidity_sweep",
        _latest_sweep,
    )

    prepared = _prepared(frame, settings=settings, price=100.2)
    signal = LiquiditySweepSetup(SetupParams(), settings).detect(prepared, settings)

    assert signal is not None
    assert signal.direction == "short"
    assert signal.entry_mid == pytest.approx(100.2)


def test_order_block_applies_single_context_mismatch_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    opens = [100.0] * 12
    closes = [100.0] * 12
    closes[6] = 104.0
    closes[7] = 105.0
    closes[8] = 106.0
    frame = _feature_frame(closes, opens=opens, price=100.0, rsi=55.0)

    def _latest_ob(*_args, **_kwargs) -> SMCZone:
        return SMCZone(
            kind="order_block",
            direction="long",
            top=101.0,
            bottom=99.0,
            created_index=5,
            state="fresh",
            midpoint=100.0,
            width=2.0,
        )

    monkeypatch.setattr("bot.strategies.order_block.latest_order_block", _latest_ob)
    monkeypatch.setattr(
        "bot.strategies.order_block.build_structural_targets",
        lambda **_kwargs: (98.0, 106.0, 110.0),
    )

    prepared = _prepared(
        frame,
        settings=settings,
        price=100.0,
        bias_1h="downtrend",
        structure_1h="downtrend",
    )
    signal = OrderBlockSetup(SetupParams(), settings).detect(prepared, settings)

    expected_base = _compute_dynamic_score(
        direction="long",
        base_score=0.52,
        vol_ratio=1.4,
        rsi=55.0,
    )
    assert signal is not None
    assert signal.score == pytest.approx(expected_base * 0.75)


def test_wick_trap_reversal_scans_latest_15m_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    frame_1h = _feature_frame(
        [101.0, 100.5, 100.0, 99.5, 95.0, 99.0, 100.0, 101.0, 102.0, 103.0],
        lows=[100.0, 99.8, 99.2, 98.8, 95.0, 98.5, 99.0, 100.0, 101.0, 102.0],
        highs=[102.0, 101.0, 100.5, 100.0, 97.0, 100.0, 101.0, 102.0, 103.0, 104.0],
        price=96.0,
        atr=1.0,
    )
    frame_1h = frame_1h.with_columns(
        pl.Series(
            "time", [base + timedelta(hours=idx) for idx in range(frame_1h.height)]
        )
    )
    frame_15m = _feature_frame(
        [96.0] * 7 + [95.4],
        lows=[95.5] * 7 + [94.4],
        highs=[96.5] * 8,
        price=95.4,
        atr=1.0,
        rsi=45.0,
    )
    frame_15m = frame_15m.with_columns(
        pl.Series(
            "time",
            [
                base + timedelta(hours=5, minutes=15 * idx)
                for idx in range(frame_15m.height)
            ],
        )
    ).with_columns(pl.Series("volume_ratio20", [1.6] * frame_15m.height))

    def _swing_points_stub(frame: pl.DataFrame, **_kwargs):
        return (
            pl.Series([False] * frame.height),
            pl.Series([idx == 4 for idx in range(frame.height)]),
        )

    monkeypatch.setattr(
        "bot.strategies.wick_trap_reversal._swing_points",
        _swing_points_stub,
    )

    prepared = _prepared(frame_15m, settings=settings, price=95.4)
    prepared.work_1h = frame_1h
    signal = WickTrapReversalSetup(SetupParams(), settings).detect(prepared, settings)

    assert signal is not None
    assert signal.direction == "long"
    assert any("wick_sweep" in reason for reason in signal.reasons)


def test_oi_divergence_uses_oi_as_participation_confirmation() -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    frame = _feature_frame(
        [100.0, 100.2, 100.4, 100.6, 101.0, 101.3, 101.7, 102.0, 102.4],
        price=102.4,
        atr=1.0,
    )
    prepared = _prepared(frame, settings=settings, price=102.4)
    prepared.oi_change_pct = 2.0

    signal = OIDivergenceSetup(SetupParams(), settings).detect(prepared, settings)

    assert signal is not None
    assert signal.direction == "long"
    assert "oi_confirms_price" in signal.reasons


def test_oi_divergence_fades_price_move_when_oi_contracts() -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    frame = _feature_frame(
        [100.0, 100.2, 100.4, 100.6, 101.0, 101.3, 101.7, 102.0, 102.4],
        price=102.4,
        atr=1.0,
    )
    prepared = _prepared(frame, settings=settings, price=102.4)
    prepared.oi_change_pct = -2.0

    signal = OIDivergenceSetup(SetupParams(), settings).detect(prepared, settings)

    assert signal is not None
    assert signal.direction == "short"
    assert "price_up_oi_contracting" in signal.reasons


def test_smc_unmitigated_fvg_and_unswept_liquidity_use_null_indices() -> None:
    fvg_frame = pl.DataFrame(
        {
            "open": [10.0, 10.2, 11.2, 11.4],
            "high": [10.2, 10.5, 11.5, 11.7],
            "low": [9.8, 10.0, 11.0, 11.2],
            "close": [10.1, 10.4, 11.3, 11.5],
            "volume": [100.0, 100.0, 100.0, 100.0],
        }
    )

    zones = fvg(fvg_frame, join_consecutive=False)
    zone = latest_fvg_zone(fvg_frame, join_consecutive=False, allowed_states=("fresh",))

    assert math.isnan(float(zones.item(1, "MitigatedIndex")))
    assert zone is not None
    assert zone.state == "fresh"
    assert zone.mitigation_index is None

    pool_frame = pl.DataFrame(
        {
            "open": [10.0, 10.1, 10.0, 10.1, 10.0],
            "high": [10.0, 10.0, 10.0, 10.0, 10.0],
            "low": [9.5, 9.6, 9.5, 9.6, 9.5],
            "close": [9.8, 9.9, 9.8, 9.9, 9.8],
            "volume": [100.0] * 5,
        }
    )
    swings = pl.DataFrame(
        {
            "HighLow": [1.0, None, 1.0, None, None],
            "Level": [10.0, None, 10.0, None, None],
        }
    )
    pools = liquidity_pools(pool_frame, swings, range_percent=0.05)

    assert pools.item(0, "Liquidity") == pytest.approx(1.0)
    assert math.isnan(float(pools.item(0, "Swept")))
