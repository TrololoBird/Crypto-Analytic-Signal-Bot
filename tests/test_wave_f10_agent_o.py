"""Wave F10 agent O: prescore wash/spread/outcome gates, enrichment stale flags, SL decay."""

from __future__ import annotations

import time

import pytest

from bot.domain.config import BotSettings
from bot.market.outcome_derank import decay_weight, penalties_from_sl_counts
from bot.market.rest_impl import BinanceClientImpl
from bot.market.universe import _prescore_row, select_light_pool_rows


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _row(
    symbol: str,
    *,
    quote_volume: float = 100_000_000.0,
    trade_count: int = 50_000,
    spread_bps: float | None = None,
) -> dict[str, float | str | None]:
    row: dict[str, float | str | None] = {
        "symbol": symbol,
        "quote_volume": quote_volume,
        "trade_count": trade_count,
    }
    if spread_bps is not None:
        row["spread_bps"] = spread_bps
    return row


def test_prescore_wash_volume_prefers_larger_avg_trade() -> None:
    settings = _settings()
    organic = _row("AAAUSDT", quote_volume=200_000_000.0, trade_count=20_000)
    wash = _row("BBBUSDT", quote_volume=200_000_000.0, trade_count=2_000_000)
    assert _prescore_row(organic, settings) > _prescore_row(wash, settings)


def test_select_light_pool_spread_gate_rejects_wide_spread() -> None:
    settings = _settings(universe={"shortlist_spread_max_bps": 8.0})
    pinned: set[str] = set()
    rows = [
        _row("TIGHTUSDT", quote_volume=300_000_000.0, spread_bps=4.0),
        _row("WIDEUSDT", quote_volume=250_000_000.0, spread_bps=20.0),
        _row("UNKUSDT", quote_volume=200_000_000.0),
    ]
    selected, stats = select_light_pool_rows(
        rows,
        settings=settings,
        pinned=pinned,
        priority_symbols=set(),
    )
    symbols = {str(row["symbol"]) for row in selected}
    assert "WIDEUSDT" not in symbols
    assert stats["spread_gate_rejected"] == 1
    assert len(selected) == 2


def test_select_light_pool_outcome_penalty_deranks_prescore() -> None:
    settings = _settings()
    rows = [
        _row("AAAUSDT", quote_volume=200_000_000.0),
        _row("BBBUSDT", quote_volume=180_000_000.0),
    ]
    baseline, _ = select_light_pool_rows(
        rows,
        settings=settings,
        pinned=set(),
        priority_symbols=set(),
    )
    penalized, _ = select_light_pool_rows(
        rows,
        settings=settings,
        pinned=set(),
        priority_symbols=set(),
        outcome_penalties={"AAAUSDT": 0.35},
    )
    assert baseline[0]["symbol"] == "AAAUSDT"
    assert penalized[0]["symbol"] == "BBBUSDT"
    assert {row["symbol"] for row in baseline} == {row["symbol"] for row in penalized}


def test_penalties_from_sl_counts_applies_time_decay() -> None:
    fresh = penalties_from_sl_counts(
        {"BTCUSDT": 3},
        sl_event_ages_days={"BTCUSDT": [0.5, 1.0, 1.5]},
        cluster_threshold=2,
        penalty_per_sl=0.08,
        max_penalty=0.28,
    )
    stale = penalties_from_sl_counts(
        {"BTCUSDT": 3},
        sl_event_ages_days={"BTCUSDT": [6.0, 6.5, 7.0]},
        cluster_threshold=2,
        penalty_per_sl=0.08,
        max_penalty=0.28,
    )
    assert fresh.get("BTCUSDT", 0.0) > stale.get("BTCUSDT", 0.0)
    assert decay_weight(7.0) < decay_weight(0.5)


def test_rest_enrichment_stale_flags_use_cache_ttl() -> None:
    client = BinanceClientImpl()
    now = time.monotonic()
    client._open_interest_cache["BTCUSDT"] = (now - 900.0, 1_000.0)
    client._taker_ratio_cache[("BTCUSDT", "1h")] = (now - 10.0, 1.05)
    flags = client.get_rest_enrichment_stale_flags("BTCUSDT")
    assert "oi_current" in flags
    assert "taker_ratio" not in flags
