from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from bot.config import BotSettings
from bot.models import PreparedSymbol, UniverseSymbol
from bot.public_intelligence import PublicIntelligenceService
from bot.setup_base import SetupParams
from bot.setups import _compute_dynamic_score
from bot.setups.smc import SMCZone
from bot.strategies.liquidity_sweep import LiquiditySweepSetup
from bot.strategies.order_block import OrderBlockSetup


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
