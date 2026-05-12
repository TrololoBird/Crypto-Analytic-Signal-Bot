from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from bot.application.market_context_updater import MarketContextUpdater
from bot.core.memory.repository import MemoryRepository
from bot.domain.schemas import UniverseSymbol


@dataclass
class _RegimeResult:
    regime: str = "bull"
    strength: float = 0.7
    btc_bias: str = "uptrend"
    eth_bias: str = "uptrend"
    altcoin_season_index: float = 63.0
    btc_phase: str = "markup"
    confidence: float = 0.8


class _Repo:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def update_market_context(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None


class _Telemetry:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def append_jsonl(self, name: str, row: dict) -> None:
        self.rows.append((name, row))


class _Client:
    def __init__(self) -> None:
        self._funding_rate_cache = {"BTCUSDT": (0, 0.0002)}

    def get_cached_oi_change(self, symbol: str, period: str = "1h"):
        return 0.01

    def get_cached_basis(self, symbol: str, period: str = "1h"):
        return 0.02

    def get_cached_basis_stats(self, symbol: str, period: str = "5m"):
        return {"premium_slope_5m": 0.001, "premium_zscore_5m": 0.2}

    async def fetch_ticker_24h(self):
        return [{"symbol": "BTCUSDT", "price_change_percent": "1.0"}]


@pytest.mark.asyncio
async def test_logs_regime_transition_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.application import market_context_updater as mcu_module

    class _DummyBinance(_Client):
        pass

    monkeypatch.setattr(mcu_module, "BinanceFuturesMarketData", _DummyBinance)

    repo = _Repo()
    bot = SimpleNamespace(
        client=_DummyBinance(),
        _ws_manager=None,
        market_regime=SimpleNamespace(analyze=lambda *args, **kwargs: _RegimeResult()),
        _modern_repo=repo,
        telemetry=_Telemetry(),
        settings=SimpleNamespace(
            intelligence=SimpleNamespace(source_policy="binance_only", regime_detector="composite")
        ),
        intelligence=None,
    )

    updater = MarketContextUpdater(bot)
    shortlist = [
        UniverseSymbol(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            contract_type="PERPETUAL",
            status="TRADING",
            onboard_date_ms=0,
            quote_volume=1_000_000,
            price_change_pct=0.0,
            last_price=100.0,
        )
    ]

    await updater.update_memory_market_context(shortlist)
    await updater.update_memory_market_context(shortlist)

    transition_rows = [r for name, r in bot.telemetry.rows if name == "regime_transitions.jsonl"]
    assert len(transition_rows) == 1
    assert transition_rows[0]["new_regime"] == "bull"
    assert repo.calls
    assert repo.calls[-1][1]["altcoin_season_index"] == pytest.approx(63.0)
    assert repo.calls[-1][1]["btc_phase"] == "markup"


@pytest.mark.asyncio
async def test_market_context_repository_persists_multi_asset_fields(tmp_path) -> None:
    repo = MemoryRepository(tmp_path / "memory.sqlite", tmp_path / "data")
    await repo.initialize()
    try:
        await repo.update_market_context(
            "uptrend",
            "downtrend",
            ["BTCUSDT"],
            ["ETHUSDT"],
            market_regime="volatile",
            market_regime_confirmed=True,
            altcoin_season_index=37.5,
            btc_phase="distribution",
        )

        context = await repo.get_market_context()

        assert context["altcoin_season_index"] == pytest.approx(37.5)
        assert context["btc_phase"] == "distribution"
    finally:
        await repo.close()
