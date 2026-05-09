from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from bot import features


def _frame(rows: int = 120) -> pl.DataFrame:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = [base + timedelta(minutes=15 * i) for i in range(rows)]
    close = [100.0 + 0.1 * i + ((i % 7) - 3) * 0.03 for i in range(rows)]
    return pl.DataFrame(
        {
            "open_time": times,
            "close_time": times,
            "open": [c - 0.4 for c in close],
            "high": [c + 0.7 for c in close],
            "low": [c - 0.7 for c in close],
            "close": close,
            "volume": [1000.0 + i for i in range(rows)],
            "quote_volume": [100000.0 + i for i in range(rows)],
            "trades": [100 + i for i in range(rows)],
            "taker_buy_base_volume": [450.0 + i * 0.2 for i in range(rows)],
            "taker_buy_quote_volume": [45000.0 + i * 3.0 for i in range(rows)],
        }
    )


def test_group_apis_exposed() -> None:
    assert "add_core_features" in features.CORE_API
    assert "add_advanced_indicators" in features.ADVANCED_API
    assert "add_oscillator_features" in features.OSCILLATORS_API


def test_group_contract_outputs() -> None:
    frame = _frame()
    core = features.CORE_API["add_core_features"](
        frame, plta=features.plta, has_talib=features._HAS_TALIB
    )
    assert {"ema20", "adx14", "vwap", "close_position"}.issubset(core.columns)
    assert float(core["rsi14"].tail(1).item()) > 1.0
    assert float(core["adx14"].tail(1).item()) > 1.0

    advanced = features.ADVANCED_API["add_advanced_indicators"](
        core,
        plta=features.plta,
        has_talib=features._HAS_TALIB,
        atr_fn=features.CORE_API["atr"],
        roc_fn=features.CORE_API["roc"],
        log_fallback=lambda *_a, **_k: None,
    )
    assert {"supertrend_dir", "bb_pct_b", "kc_width"}.issubset(advanced.columns)

    oscillators = features.OSCILLATORS_API["add_oscillator_features"](
        advanced,
        plta=features.plta,
        has_talib=features._HAS_TALIB,
    )
    assert {"stoch_k14", "mfi14", "uo"}.issubset(oscillators.columns)
