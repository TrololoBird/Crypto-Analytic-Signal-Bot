"""Unit tests for shortlist strategy pools (no network)."""

from __future__ import annotations

from bot.domain.config import BotSettings
from bot.domain.schemas import UniverseSymbol
from bot.market.strategy_pools import (
    asset_strategy_allowlist,
    decorrelation_key,
    fill_shortlist_from_pools,
    scaled_pool_targets,
)


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _symbol(
    symbol: str,
    *,
    score: float,
    move: float,
    rank: int,
    fits: tuple[str, ...],
) -> UniverseSymbol:
    return UniverseSymbol(
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=100_000_000.0,
        price_change_pct=move,
        last_price=1.0,
        trade_count_24h=10_000,
        shortlist_bucket="trend",
        shortlist_score=score,
        shortlist_reasons=("unit",),
        seed_source="unit",
        liquidity_rank=rank,
        strategy_fits=fits,
    )


def test_scaled_pool_targets_bear_regime_weights_positioning() -> None:
    targets = scaled_pool_targets(30, market_regime="bear")
    assert sum(targets.values()) == 30
    assert targets["positioning"] >= targets["klines"]


def test_decorrelation_key_limits_clone_moves() -> None:
    a = _symbol("AAAUSDT", score=0.8, move=5.1, rank=5, fits=("ema_bounce",))
    b = _symbol("BBBUSDT", score=0.79, move=5.4, rank=6, fits=("ema_bounce",))
    assert decorrelation_key(a) == decorrelation_key(b)


def test_fill_shortlist_respects_decorrelation_cap() -> None:
    dynamic = [
        _symbol(f"ALT{i}USDT", score=0.9 - i * 0.01, move=5.0, rank=i + 1, fits=("ema_bounce",))
        for i in range(6)
    ]
    shortlist: list[UniverseSymbol] = []
    seen: set[str] = set()
    summary = fill_shortlist_from_pools(
        shortlist=shortlist,
        seen=seen,
        dynamic_pool=dynamic,
        shortlist_limit=6,
        setup_ids=("ema_bounce",),
        market_regime="bear",
    )
    assert len(shortlist) <= 2
    assert summary["decorrelation_skips"] >= 0


def test_asset_strategy_allowlist_honors_excluded() -> None:
    settings = _settings(
        assets={
            "BTCUSDT": {
                "allowed_strategies": ["structure_pullback", "btc_correlation", "order_block"],
                "excluded_strategies": ["order_block"],
            }
        }
    )
    allowed = asset_strategy_allowlist(
        "BTCUSDT",
        settings=settings,
        enabled={"structure_pullback", "btc_correlation", "order_block"},
        heuristic_fits=("structure_pullback", "order_block"),
    )
    assert set(allowed) == {"structure_pullback", "btc_correlation"}
    assert "order_block" not in allowed
